from flask import Flask, request, send_from_directory, make_response
import sys, fcntl, os.path, json, time, base64, pysodium, re
import sqlite3 as db

#====
from shttpfs.common import cpjoin, file_get_contents
from shttpfs.versioned_storage import versioned_storage
from shttpfs.merge_client_and_server_changes import merge_client_and_server_changes

app = Flask(__name__)
app.debug = True

#===============================================================================
conf_path = '/etc/shttpfs/server.json'
if len(sys.argv) > 1 and sys.argv[1] == '-c':
    conf_path = sys.argv[2]

config = json.loads(file_get_contents(conf_path))

#===============================================================================
lock_fail_msg        = 'Could not acquire exclusive lock'
no_such_repo_msg     = "The requested repository does not exist"
user_auth_fail_msg   = "Could not authenticate user"
conflict_msg         = 'Please resolve conflicts'
need_to_update_msg   = "Please update to latest revision"
no_active_commit_msg = "A commit must be started before attempting this operation."

extend_session_duration = (60 * 60) * 2 # 2 hours

#===============================================================================
def server_responce(headers, body):
    response = make_response(body)
    for k, v in headers.iteritems(): response.headers[k] = v
    return response


#===============================================================================
def fail(msg = ''):
    """ Generate fail JSON to send to client """
    return server_responce({'status' : 'fail', 'msg' : msg}, '')


#===============================================================================
def success(headers = None, data = ''):
    """ Generate success JSON to send to client """
    passed_headers = {} if headers is None else headers
    if isinstance(data, dict): data = json.dumps(data)
    ret_headers = {'status' : 'ok'}
    ret_headers.update(headers)
    return server_responce(ret_headers, data)


#===============================================================================
#  Locking
#
# Locking is only required for writing as clients will read the prior version
# while a commit is in progress and this updates atomically. This uses flock
# as a convenient way to synchronise across multiple processes.
#===============================================================================
def lock_access(repository_path, callback):
    """ Synchronise access to the user file between processes, this specifies
    which user is allowed write access at the current time """

    with open(cpjoin(repository_path, 'lock_file'), 'w') as fd:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            returned = callback()
            fcntl.flock(fd, fcntl.LOCK_UN)
            return returned
        except IOError:
            return fail(lock_fail_msg)


#===============================================================================
def update_user_lock(repository_path, session_token):
    """ Write or clear the user lock file """ # NOTE ALWAYS use within lock access callback

    # While the user lock file should ALWAYS be written only within a lock_access
    # callback, it is sometimes read asynchronously. Because of this updates to
    # the file must be atomic. Write plus move is used to achieve this.
    real_path = cpjoin(repository_path, 'user_file')
    tmp_path  = cpjoin(repository_path, 'new_user_file')

    with open(tmp_path, 'w') as fd2:
        if session_token is None: fd2.write('')
        else: fd2.write(json.dumps({'session_token' : session_token, 'expires' : int(time.time()) + 30}))
        fd2.flush()
    os.rename(tmp_path, real_path)


#===============================================================================
def can_aquire_user_lock(repository_path, session_token):
    """ Allow a user to acquire the lock if no other user is currently using it, if the original
    user is returning, presumably after a network error, or if the lock has expired.  """
    # NOTE ALWAYS use within lock access callback

    user_file_path = cpjoin(repository_path, 'user_file')
    if not os.path.isfile(user_file_path): return True
    with open(user_file_path, 'r') as fd2:
        content = fd2.read()
        if len(content) == 0: return True
        try: res = json.loads(content)
        except ValueError: return True
        if res['expires'] < int(time.time()): return True
        elif res['session_token'] == session_token: return True
    return False

#===============================================================================
def read_user_lock(repository_path):
    try:
        user_lock = file_get_contents(cpjoin(repository_path, 'user_file'))
        if user_lock == '': return None
        return json.loads(user_lock)
    except IOError: return None
    except ValueError: return None


#===============================================================================
def varify_user_lock(repository_path, session_token):
    """ Verify that a returning user has a valid token and their lock has not expired """

    with open(cpjoin(repository_path, 'user_file'), 'r') as fd2:
        content = fd2.read()
        if len(content) == 0: return False
        try: res = json.loads(content)
        except ValueError: return False
        return res['session_token'] == session_token and int(time.time()) < int(res['expires'])
    return False


#===============================================================================
# Authentication
#===============================================================================
def auth_db_connect(db_path):
    """ An SQLite database is used to store authentication transient data,
    this is tokens, strings of random data which are signed by the client,
    and session_tokens which identify authenticated users """

    def dict_factory(cursor, row): return {col[0] : row[idx] for idx,col in enumerate(cursor.description)}
    conn = db.connect(db_path)
    conn.row_factory = dict_factory
    if not auth_db_connect.init:
        conn.execute('create table if not exists tokens (expires int, token text, ip text)')
        conn.execute('create table if not exists session_tokens (expires int, token text, ip text, username text)')
        auth_db_connect.init = True
    return conn
auth_db_connect.init = False

#===============================================================================
def gc_tokens(conn):
    """ Garbage collection for expired authentication tokens """

    conn.execute("delete from tokens where expires < ?", (time.time(),))
    conn.commit()

#===============================================================================
@app.route('/begin_auth', methods=['POST'])
def begin_auth():
    """ Request authentication token to sign """

    repository    = request.headers['repository']
    if repository not in config['repositories']: return fail(no_such_repo_msg)

    # ==
    repository_path = config['repositories'][repository]['path']
    conn = auth_db_connect(cpjoin(repository_path, 'auth_transient.db')); gc_tokens(conn)

    # Issue a new token
    auth_token = base64.b64encode(pysodium.randombytes(35)).decode('utf-8')
    conn.execute("insert into tokens (expires, token, ip) values (?,?,?)",
                 (time.time() + 30, auth_token, request.environ['REMOTE_ADDR']))
    conn.commit()

    return success({'auth_token' : auth_token})


#===============================================================================
@app.route('/authenticate', methods=['POST'])
def authenticate():
    """ This does two things, either validate a pre-existing session token
    or create a new one from a signed authentication token. """

    client_ip     = request.environ['REMOTE_ADDR']
    repository    = request.headers['repository']
    if repository not in config['repositories']: return fail(no_such_repo_msg)

    # ==
    repository_path = config['repositories'][repository]['path']
    conn = auth_db_connect(cpjoin(repository_path, 'auth_transient.db')); gc_tokens(conn)
    gc_tokens(conn)

    # Allow resume of an existing session
    if 'session_token' in request.headers:
        session_token = request.headers['session_token']

        conn.execute("delete from session_tokens where expires < ?", (time.time(),)); conn.commit()
        res = conn.execute("select * from session_tokens where token = ? and ip = ?", (session_token, client_ip)).fetchall()
        if res != []: return success({'session_token'  : session_token})
        else:         return fail(user_auth_fail_msg)

    # Create a new session
    else:
        user       = request.headers['user']
        auth_token = request.headers['auth_token']
        signiture  = request.headers['signature']

        try:
            public_key = config['users'][user]['public_key']

            # signature
            pysodium.crypto_sign_verify_detached(base64.b64decode(signiture), auth_token, base64.b64decode(public_key))

            # check token was previously issued by this system and is still valid
            res = conn.execute("select * from tokens where token = ? and ip = ? ", (auth_token, client_ip)).fetchall()

            # Validate token matches one we sent
            if res == [] or len(res) > 1: return fail(user_auth_fail_msg)

            # Does the user have permission to use this repository?
            if repository not in config['users'][user]['uses_repositories']: return fail(user_auth_fail_msg)

            # Everything OK
            conn.execute("delete from tokens where token = ?", (auth_token,)); conn.commit()

            # generate a session token and send it to the client
            session_token = base64.b64encode(pysodium.randombytes(35))
            conn.execute("insert into session_tokens (token, expires, ip, username) values (?,?,?, ?)",
                         (session_token, time.time() + extend_session_duration, client_ip, user))
            conn.commit()
            return success({'session_token'  : session_token})

        except Exception: # pylint: disable=broad-except
            return fail(user_auth_fail_msg)


#===============================================================================
def have_authenticated_user(client_ip, repository, session_token):
    """ check user submitted session token against the db and that ip has not changed """

    if repository not in config['repositories']: return False

    repository_path = config['repositories'][repository]['path']
    conn = auth_db_connect(cpjoin(repository_path, 'auth_transient.db'))

    # Garbage collect session tokens. We must not garbage collect the authentication token of the client
    # which is currently doing a commit. Large files can take a long time to upload and during this time,
    # the locks expiration is not being updated thus can expire. This is a problem here as session tokens
    # table is garbage collected every time a user authenticates. It does not matter if the user_lock
    # expires while the client also holds the flock, as it is updated to be in the future at the end of
    # the current operation. We exclude any tokens owned by the client which currently owns the user
    # lock for this reason.
    user_lock = read_user_lock(repository_path)
    active_commit = user_lock['session_token'] if user_lock != None else None

    if active_commit != None: conn.execute("delete from session_tokens where expires < ? and token != ?", (time.time(), active_commit))
    else:                     conn.execute("delete from session_tokens where expires < ?", (time.time(),))

    # Get the session token
    res = conn.execute("select * from session_tokens where token = ? and ip = ?", (session_token, client_ip)).fetchall()

    if res != [] and repository in config['users'][res[0]['username']]['uses_repositories']:
        conn.execute("update session_tokens set expires = ? where token = ? and ip = ?",
                     (time.time() + extend_session_duration, session_token, client_ip))

        conn.commit() # to make sure the update and delete have the same view

        return res[0]

    conn.commit()
    return False


#===============================================================================
# Main System
#===============================================================================
@app.route('/find_changed', methods=['POST'])
def find_changed():
    """ Find changes since the revision it is currently holding """

    session_token = request.headers['session_token']
    repository    = request.headers['repository']

    #===
    current_user = have_authenticated_user(request.environ['REMOTE_ADDR'], repository, session_token)
    if current_user is False: return fail(user_auth_fail_msg)

    #===
    repository_path = config['repositories'][repository]['path']
    body_data = request.get_json()

    #===
    data_store = versioned_storage(repository_path)
    head = data_store.get_head()
    if head == 'root': return success({}, {'head' : 'root', 'sorted_changes' : {'none' : []}})


    # Find changed items
    client_changes = json.loads(body_data['client_changes'])
    server_changes = data_store.get_changes_since(request.headers["previous_revision"], head)

    # Resolve conflicts
    conflict_resolutions = json.loads(body_data['conflict_resolutions'])
    if conflict_resolutions != []:
        resolutions = {'server' : {},'client' : {}}
        for r in conflict_resolutions:
            if len(r['4_resolution']) != 1 or r['4_resolution'][0] not in ['client', 'server']: return fail(conflict_msg)
            resolutions[r['4_resolution'][0]][r['1_path']] = None

        client_changes = {k : v for k,v in client_changes.iteritems() if v['path'] not in resolutions['server']}
        server_changes = {k : v for k,v in server_changes.iteritems() if v['path'] not in resolutions['client']}

    sorted_changes = merge_client_and_server_changes(server_changes, client_changes)
    return success({}, {'head' : head, 'sorted_changes': sorted_changes})


#===============================================================================
@app.route('/pull_file', methods=['POST'])
def pull_file():
    """ Get a file from the server """

    session_token = request.headers['session_token']
    repository    = request.headers['repository']

    #===
    current_user = have_authenticated_user(request.environ['REMOTE_ADDR'], repository, session_token)
    if current_user is False: return fail(user_auth_fail_msg)


    #===
    data_store = versioned_storage(config['repositories'][repository]['path'])
    file_info = data_store.get_file_info_from_path(request.headers['path'])

    return success({'file_info_json' : json.dumps(file_info)},
                   send_from_directory(data_store.get_file_directory_path(file_info['hash']), file_info['hash'][2:]))


#===============================================================================
@app.route('/list_versions', methods=['POST'])
def list_versions():
    session_token = request.headers['session_token']
    repository    = request.headers['repository']

    #===
    current_user = have_authenticated_user(request.environ['REMOTE_ADDR'], repository, session_token)
    if current_user is False: return fail(user_auth_fail_msg)

    #===
    data_store = versioned_storage(config['repositories'][repository]['path'])
    return success({}, {'versions' : data_store.get_commit_chain()})


#===============================================================================
@app.route('/list_changes', methods=['POST'])
def list_changes():
    session_token = request.headers['session_token']
    repository    = request.headers['repository']

    #===
    current_user = have_authenticated_user(request.environ['REMOTE_ADDR'], repository, session_token)
    if current_user is False: return fail(user_auth_fail_msg)

    #===
    repository_path = config['repositories'][repository]['path']

    data_store = versioned_storage(config['repositories'][repository]['path'])
    return success({}, {'changes' : data_store.get_commit_changes(request.headers['version_id'])})


#===============================================================================
@app.route('/list_files', methods=['POST'])
def list_files():
    session_token = request.headers['session_token']
    repository    = request.headers['repository']

    #===
    current_user = have_authenticated_user(request.environ['REMOTE_ADDR'], repository, session_token)
    if current_user is False: return fail(user_auth_fail_msg)

    #===
    data_store = versioned_storage(config['repositories'][repository]['path'])
    return success({}, {'files' : data_store.get_commit_files(request.headers['version_id'])})


#===============================================================================
@app.route('/begin_commit', methods=['POST'])
def begin_commit():
    """ Allow a client to begin a commit and acquire the write lock """

    session_token = request.headers['session_token']
    repository    = request.headers['repository']

    #===
    current_user = have_authenticated_user(request.environ['REMOTE_ADDR'], repository, session_token)
    if current_user is False: return fail(user_auth_fail_msg)

    #===
    repository_path = config['repositories'][repository]['path']

    def with_exclusive_lock():
        # The commit is locked for a given time period to a given session token,
        # a client must hold this lock to use any of push_file(), delete_files() and commit().
        # It does not matter if the user lock technically expires while a client is writing
        # a large file, as the user lock is locked using flock for the duration of any
        # operation and thus cannot be stolen by another client. It is updated to be in
        # the future before returning to the client. The lock only needs to survive until
        # the client owning the lock sends another request and re acquires the flock.
        if not can_aquire_user_lock(repository_path, session_token): return fail(lock_fail_msg)

        # Commits can only take place if the committing user has the latest revision,
        # as committing from an outdated state could cause unexpected results, and may
        # have conflicts. Conflicts are resolved during a client update so they are
        # handled by the client, and a server interface for this is not needed.
        data_store = versioned_storage(repository_path)
        if data_store.get_head() != request.headers["previous_revision"]: return fail(need_to_update_msg)


        # Should the lock expire, the client which had the lock previously will be unable
        # to continue the commit it had in progress. When this, or another client attempts
        # to commit again it must do so by first obtaining the lock again by calling begin_commit().
        # Any remaining commit data from failed prior commits is garbage collected here.
        # While it would technically be possible to implement commit resume should the same
        # client resume, I only see commits failing due to a network error and this is so
        # rare I don't think it's worth the trouble.
        if data_store.have_active_commit(): data_store.rollback()

        #------------
        data_store.begin()
        update_user_lock(repository_path, session_token)

        return success()
    return lock_access(repository_path, with_exclusive_lock)



#===============================================================================
@app.route('/push_file', methods=['POST'])
def push_file():
    """ Push a file to the server """ #NOTE beware that reading post data in flask causes hang until file upload is complete

    session_token = request.headers['session_token']
    repository    = request.headers['repository']

    #===
    current_user = have_authenticated_user(request.environ['REMOTE_ADDR'], repository, session_token)
    if current_user is False: return fail(user_auth_fail_msg)

    #===
    repository_path = config['repositories'][repository]['path']

    def with_exclusive_lock():
        if not varify_user_lock(repository_path, session_token): return fail(lock_fail_msg)

        #===
        data_store = versioned_storage(repository_path)
        if not data_store.have_active_commit(): return fail(no_active_commit_msg)

        # There is no valid reason for path traversal characters to be in a file path within this system
        file_path = request.headers['path']
        if any(True for item in re.split(r'\\|/', file_path) if item in ['..', '.']): return fail()

        #===
        tmp_path = cpjoin(repository_path, 'tmp_file')
        with open(tmp_path, 'wb') as f:
            while True:
                chunk = request.stream.read(1000 * 1000)
                if chunk == b'': break
                f.write(chunk)

        #===
        data_store.fs_put_from_file(tmp_path, {'path' : file_path})

        # updates the user lock expiry
        update_user_lock(repository_path, session_token)
        return success()

    return lock_access(repository_path, with_exclusive_lock)


#===============================================================================
@app.route('/delete_files', methods=['POST'])
def delete_files():
    """ Delete one or more files from the server """

    session_token = request.headers['session_token']
    repository    = request.headers['repository']

    #===
    current_user = have_authenticated_user(request.environ['REMOTE_ADDR'], repository, session_token)
    if current_user is False: return fail(user_auth_fail_msg)

    #===
    repository_path = config['repositories'][repository]['path']
    body_data = request.get_json()

    def with_exclusive_lock():
        if not varify_user_lock(repository_path, session_token): return fail(lock_fail_msg)

        try:
            data_store = versioned_storage(repository_path)
            if not data_store.have_active_commit(): return fail(no_active_commit_msg)

            #-------------
            for fle in json.loads(body_data['files']):
                data_store.fs_delete(fle)

            # updates the user lock expiry
            update_user_lock(repository_path, session_token)
            return success()
        except Exception as e: return fail() # pylint: disable=broad-except
    return lock_access(repository_path, with_exclusive_lock)


#===============================================================================
@app.route('/commit', methods=['POST'])
def commit():
    """ Commit changes and release the write lock """

    session_token = request.headers['session_token']
    repository    = request.headers['repository']

    #===
    current_user = have_authenticated_user(request.environ['REMOTE_ADDR'], repository, session_token)
    if current_user is False: return fail(user_auth_fail_msg)

    #===
    repository_path = config['repositories'][repository]['path']

    def with_exclusive_lock():
        if not varify_user_lock(repository_path, session_token): return fail(lock_fail_msg)

        #===
        data_store = versioned_storage(repository_path)
        if not data_store.have_active_commit(): return fail(no_active_commit_msg)

        result = {}
        if request.headers['mode'] == 'commit':
            new_head = data_store.commit(request.headers['commit_message'], current_user['username'])
            result = {'head' : new_head}
        else:
            data_store.rollback()

        # Release the user lock
        update_user_lock(repository_path, None)
        return success(result)
    return lock_access(repository_path, with_exclusive_lock)

