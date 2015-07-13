#!/usr/bin/env python
# coding=utf-8
'''
Asynchronous DNS client to query a queue of domains asynchronously.
This is designed to improve performance of the server.
'''
import asyncio, os, logging
from . import utils, types

A_TYPES = types.A, types.AAAA

cachefile = os.path.expanduser('~/.gerald/named.cache.txt')
def get_name_cache(url = 'ftp://rs.internic.net/domain/named.cache',
        fname = cachefile):
    from urllib import request
    logging.info('Fetching named.cache...')
    try:
        r = request.urlopen(url)
    except:
        logging.warning('Error fetching named.cache')
    else:
        open(fname, 'wb').write(r.read())
def get_root_servers(fname = cachefile):
    if not os.path.isfile(fname):
        os.makedirs(os.path.dirname(fname), exist_ok = True)
        get_name_cache(fname = fname)
    # in case failed fetching named.cache
    if os.path.isfile(fname):
        for line in open(fname, 'r'):
            if line.startswith(';'): continue
            it = iter(filter(None, line.split()))
            data = [next(it).rstrip('.')]   # name
            expires = next(it)  # ignored
            data.append(types.MAP_TYPES.get(next(it), 0))   # qtype
            data.append(next(it).rstrip('.'))   # data
            yield data

class CallbackProtocol(asyncio.DatagramProtocol):
    def __init__(self, future):
        self.future = future

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        self.transport.close()
        if not self.future.cancelled():
            self.future.set_result((data, addr))

class DNSMemCache(utils.Hosts):
    name = 'DNSMemD/Gerald'
    def __init__(self, filename = None):
        super().__init__(filename)
        self.add_item('1.0.0.127.in-addr.arpa', types.PTR, self.name)
        for i in get_root_servers():
            self.add_item(*i)

    def add_item(self, key, qtype, data):
        self.add_host(key, utils.Record(name = key, data = data, qtype = qtype, ttl = -1))

class AsyncResolver:
    recursion_available = 1
    rootdomains = ['.lan']
    def __init__(self):
        self.queue = asyncio.Queue()
        self.futures = {}
        self.lock = asyncio.Lock()
        self.cache = DNSMemCache()
        asyncio.ensure_future(self.loop())

    @asyncio.coroutine
    def query_future(self, fqdn, qtype = types.ANY):
        key = fqdn, qtype
        with (yield from self.lock):
            future = self.futures.get(key)
            if future is None:
                future = self.futures[key] = asyncio.Future()
                yield from self.queue.put(key)
        return future

    @asyncio.coroutine
    def query_cache(self, res, fqdn, qtype):
        # cached CNAME
        cname = list(self.cache.query(fqdn, types.CNAME))
        if cname:
            res.an.extend(cname)
            if not self.recursion_available or qtype == types.CNAME:
                return True
            for rec in cname:
                cres = yield from self.query(rec.data, qtype)
                if cres is None or cres.r > 0: continue
                res.an.extend(cres.an)
                res.ns = cres.ns
                res.ar = cres.ar
            return True
        # cached else
        data = list(self.cache.query(fqdn, qtype))
        n = 0
        if data:
            for rec in data:
                if rec.qtype in (types.NS,):
                    nres = list(self.cache.query(r.data, A_TYPES))
                    empty = not nres
                    if not empty:
                        res.ar.extend(nres)
                        res.ns.append(rec)
                        if rec.qtype == qtype: n += 1
                else:
                    res.an.append(rec.copy(name = fqdn))
                    if qtype == types.CNAME or rec.qtype != types.CNAME:
                        n += 1
        if list(filter(None, map(fqdn.endswith, self.rootdomains))):
            if not n:
                res.r = 3
                n = 1
            # can only be added for domains that are resolved by this server
            res.aa = 1  # Authoritative answer
            res.ns.append(utils.Record(name = fqdn, qtype = types.NS, data = 'localhost', ttl = -1))
            res.ar.append(utils.Record(name = fqdn, qtype = types.A, data = '127.0.0.1', ttl = -1))
        if n:
            return True

    def get_nameservers(self, fqdn):
        empty = True
        while fqdn and empty:
            sub, _, fqdn = fqdn.partition('.')
            for rec in self.cache.query(fqdn, types.NS):
                host = rec.data
                if utils.ip_type(host) is None:
                    for r in self.cache.query(host, A_TYPES):
                        yield r.data
                        empty = False
                else:
                    yield host
                    empty = False

    @asyncio.coroutine
    def query_remote(self, res, fqdn, qtype):
        # look up from other DNS servers
        nsip = self.get_nameservers(fqdn)
        cname = [fqdn]
        req = utils.dns_request()
        n = 0
        while not n:
            if not cname: break
            # XXX it seems that only one qd is supported by most NS
            req.qd = [utils.Record(utils.REQUEST, cname[0], qtype)]
            qdata = req.pack()
            del cname[:]
            qid = qdata[:2]
            loop = asyncio.get_event_loop()
            for ip in nsip:
                future = asyncio.Future()
                try:
                    transport, protocol = yield from asyncio.wait_for(
                        loop.create_datagram_endpoint(lambda : CallbackProtocol(future), remote_addr = (ip, 53)),
                        1.0
                    )
                    transport.sendto(qdata)
                    data, addr = yield from asyncio.wait_for(future, 3.0)
                    transport.close()
                    if not data.startswith(qid):
                        raise utils.DNSError(-1, 'Message id does not match!')
                except asyncio.TimeoutError:
                    pass
                except utils.DNSError:
                    pass
                else:
                    break
            else:
                break
            cres = utils.raw_parse(data)
            for r in cres.an + cres.ns + cres.ar:
                if r.ttl > 0 and r.qtype not in (types.SOA, types.MX):
                    self.cache.add_host(r.name, r)
            for r in cres.an:
                res.an.append(r)
                if r.qtype == types.CNAME:
                    cname.append(r.data)
                if (r.name.lower() == req.qd[0].name.lower() and
                    (qtype == types.CNAME or r.qtype != types.CNAME)):
                    n += 1
            for r in cres.ns:
                res.ns.append(r)
                if r.qtype == types.SOA or qtype == types.NS:
                    n += 1
            res.ar.extend(cres.ar)
            nsip = [i.data for i in cres.ar if i.qtype in A_TYPES]
            if not nsip:
                for i in cres.ns:
                    host = i.data[0] if i.qtype == types.SOA else i.data
                    try:
                        # XXX is NS always a hostname? need ip_version test?
                        ns = yield from self.query(host)
                    except Exception as e:
                        logging.error(host)
                        logging.error(e)
                    else:
						if ns:
							for j in ns.an:
								if j.qtype in A_TYPES:
									nsip.append(j.data)
            if cres.r > 0:
                res.r = cres.r
        return n > 0

    @asyncio.coroutine
    def query(self, fqdn, qtype = types.ANY):
        future = yield from self.query_future(fqdn, qtype)
        try:
            res = yield from asyncio.wait_for(future, 3.0)
        except asyncio.TimeoutError:
            return
        return res

    @asyncio.coroutine
    def query_key(self, key):
        fqdn, qtype = key
        res = utils.DNSMessage(ra = self.recursion_available)
        res.qd.append(utils.Record(utils.REQUEST, name = fqdn, qtype = qtype))
        future = self.futures[key]
        ret = (yield from self.query_cache(res, fqdn, qtype)) or (yield from self.query_remote(res, fqdn, qtype))
        if not ret: res.r = 2
        with (yield from self.lock):
            self.futures.pop(key)
        if not future.cancelled():
            future.set_result(res)

    @asyncio.coroutine
    def loop(self):
        while True:
            key = yield from self.queue.get()
            asyncio.ensure_future(self.query_key(key))

class AsyncProxyResolver(AsyncResolver):
    proxies = ['114.114.114.114', '180.76.76.76', '223.5.5.5', '223.6.6.6']

    def get_nameservers(self, fdqn = None):
        return self.proxies