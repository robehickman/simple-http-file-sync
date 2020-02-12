import http.client, os, json, urllib.parse

class client_http_request(object):
############################################################################################
    def __init__(self, server_base_url):
        "Configure the servers base URL"

        res = urllib.parse.urlparse(server_base_url)
        self.scheme          = res.scheme.lower()
        self.server_base_url = res.netloc

############################################################################################
    def begin(self, url, body_length, add_headers, content_type):
        headers = {
            'Accept-Encoding': 'identity',
            'Host'           : self.server_base_url,
            'Content-Type'   : content_type,
            'Content-length' : body_length,
            'Connection'     : 'close',
            'User-Agent'     : 'SHTTPFS' }

        for k, v in add_headers.items(): headers[k] = v

        # ==
        if   self.scheme == 'http':  conn = http.client.HTTPConnection (self.server_base_url)
        elif self.scheme == 'https': conn = http.client.HTTPSConnection(self.server_base_url) # pylint: disable=redefined-variable-type
        else: raise SystemExit('unknown protocol: ' + self.scheme)

        conn.putrequest('POST', '/' + url)
        for k, v in headers.items(): conn.putheader(k, v)
        conn.endheaders()
        return conn

############################################################################################
    def request(self, url, headers, data = None, gen = False):
        jsn = json.dumps(data) if data is not None else '{}'
        conn = self.begin(url, len(jsn), headers, content_type = 'application/json')
        conn.send(jsn)
        res = conn.getresponse()

        if gen is False:
            return res.read(), dict(res.getheaders())

        else:
            def writer(path):
                with open(path, 'wb') as f:
                    while True:
                        chunk = res.read(1000 * 1000)
                        if chunk == b'': break
                        f.write(chunk)

            return writer, dict(res.getheaders())

############################################################################################
    def send_file(self, url, headers, file_path):
        size = os.stat(file_path).st_size

        conn = self.begin(url, size, headers, content_type = 'application/octet-stream')

        with open(file_path, 'rb') as f:
            while True:
                chunk = f.read(1000 * 1000)
                if chunk == b'': break
                conn.send(chunk)

        res = conn.getresponse()
        return res.read(), dict(res.getheaders())
