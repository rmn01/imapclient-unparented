# Copyright (c) 2014, Menno Smits
# Released subject to the New BSD License
# Please see http://en.wikipedia.org/wiki/BSD_licenses

from __future__ import unicode_literals

import imaplib
import itertools
import select
import socket
import sys
import re

from datetime import datetime
from operator import itemgetter

# Confusingly, this module is for OAUTH v1, not v2
try:
    import oauth2 as oauth_module
except ImportError:
    oauth_module = None

from . import response_lexer
from . import tls
from .datetime_util import datetime_to_INTERNALDATE
from .imap_utf7 import encode as encode_utf7, decode as decode_utf7
from .response_types import SearchIds
from .six import moves, iteritems, text_type, integer_types, PY3, binary_type, string_types
xrange = moves.xrange

if PY3:
    long = int  # long is just int in python3


__all__ = ['IMAPClient', 'DELETED', 'SEEN', 'ANSWERED', 'FLAGGED', 'DRAFT', 'RECENT']

from .response_parser import parse_response, parse_fetch_response

# We also offer the gmail-specific XLIST command...
if 'XLIST' not in imaplib.Commands:
    imaplib.Commands['XLIST'] = imaplib.Commands['LIST']

# ...and IDLE
if 'IDLE' not in imaplib.Commands:
    imaplib.Commands['IDLE'] = imaplib.Commands['APPEND']

# ..and STARTTLS
if 'STARTTLS' not in imaplib.Commands:
    imaplib.Commands['STARTTLS'] = ('NONAUTH',)

# ...and ID. RFC2971 says that this command is valid in all states,
# but not that some servers (*cough* FastMail *cough*) don't seem to
# accept it in state NONAUTH.
if 'ID' not in imaplib.Commands:
  imaplib.Commands['ID'] = ('NONAUTH', 'AUTH', 'SELECTED')


# System flags
DELETED = r'\Deleted'
SEEN = r'\Seen'
ANSWERED = r'\Answered'
FLAGGED = r'\Flagged'
DRAFT = r'\Draft'
RECENT = r'\Recent'         # This flag is read-only

class Namespace(tuple):
    def __new__(cls, personal, other, shared):
        return tuple.__new__(cls, (personal, other, shared))

    personal = property(itemgetter(0))
    other = property(itemgetter(1))
    shared = property(itemgetter(2))


class IMAPClient(object):
    """A connection to the IMAP server specified by *host* is made when
    this class is instantiated.

    *port* defaults to 143, or 993 if *ssl* is ``True``.

    If *use_uid* is ``True`` unique message UIDs be used for all calls
    that accept message ids (defaults to ``True``).

    If *ssl* is ``True`` an SSL connection will be made (defaults to
    ``False``).

    If *stream* is ``True`` then *host* is used as the command to run
    to establish a connection to the IMAP server (defaults to
    ``False``). This is useful for exotic connection or authentication
    setups.

    Additional keyword arguments are passed through to the constructor
    of the :py:class:`imaplib.IMAP4` class, or
    :py:class:`imaplib.IMAP4_SSL` when *ssl* is ``True`` (these are
    used by ``IMAPClient`` internally). This allows passing SSL
    related parameters such as *keyfile*, *certfile* and *ssl_context*
    (Python version dependent). For details, see the :py:mod:`imaplib`
    documentation in the standard library reference.

    .. note::

       Support for passthrough keyword arguments may be removed in
       some future version of IMAPClient. Backwards compatibility is
       not guaranteed for this feature.

    The *normalise_times* attribute specifies whether datetimes
    returned by ``fetch()`` are normalised to the local system time
    and include no timezone information (native), or are datetimes
    that include timezone information (aware). By default
    *normalise_times* is True (times are normalised to the local
    system time). This attribute can be changed between ``fetch()``
    calls if required.

    The *debug* property can be used to enable debug logging. It can
    be set to an integer from 0 to 5 where 0 disables debug output and
    5 enables full output with wire logging and parsing logs. ``True``
    and ``False`` can also be assigned where ``True`` sets debug level
    4.

    By default, debug output goes to stderr. The *log_file* attribute
    can be assigned to an alternate file handle for writing debug
    output to.

    """

    Error = imaplib.IMAP4.error
    AbortError = imaplib.IMAP4.abort
    ReadOnlyError = imaplib.IMAP4.readonly

    def __init__(self, host, port=None, use_uid=True, ssl=False, stream=False,
                 **kwargs):
        if stream:
            if port is not None:
                raise ValueError("can't set 'port' when 'stream' True")
            if ssl:
                raise ValueError("can't use 'ssl' when 'stream' is True")
        elif port is None:
            port = ssl and 993 or 143

        self.host = host
        self.port = port
        self.ssl = ssl
        self.stream = stream
        self.use_uid = use_uid
        self.folder_encode = True
        self.log_file = sys.stderr
        self.normalise_times = True

        self._starttls_done = False
        self._cached_capabilities = None
        self._imap = self._create_IMAP4(**kwargs)
        self._imap._mesg = self._log    # patch in custom debug log method
        self._idle_tag = None

    def _create_IMAP4(self, **kwargs):
        # Create the IMAP instance in a separate method to make unit tests easier
        if self.stream:
            return imaplib.IMAP4_stream(self.host)
        ImapClass = self.ssl and imaplib.IMAP4_SSL or imaplib.IMAP4
        return ImapClass(self.host, self.port, **kwargs)

    def starttls(self, ssl_context=None):
        """Switch to an SSL encrypted connection by sending a STARTTLS command.

        The *ssl_context* argument is optional and should be a
        :py:class:`OpenSSL.SSL.Context` object. If no SSL context is given, a
        default SSL context with reasonably secure settings will be created.

        You can enable checking of the hostname in the certificate presented
        by the server  against the hostname which was used for connecting, by
        setting the *check_hostname* attribute of the SSL context to ``True``.
        The default SSL context has this setting enabled.

        Raises :py:exc:`Error` if the SSL connection could not be established.

        Raises :py:exc:`AbortError` if the server does not support STARTTLS
        or an SSL connection is already established.
        """
        if self.ssl or self._starttls_done:
            raise self.abort('TLS session already established')

        typ, dat = self._imap._simple_command("STARTTLS")
        if typ != 'OK':
            raise self.Error("Couldn't establish TLS session")

        self._tls_established = True
        self._imap.sock = tls.wrap_socket(self._imap.sock, self.host, ssl_context)
        self._imap.file = self._imap.sock.makefile()
        return self._imap._untagged_response(typ, dat, "STARTTLS")

    def login(self, username, password):
        """Login using *username* and *password*, returning the
        server response.
        """
        return self._command_and_check(
            'login',
            to_bytes(username),
            to_bytes(password),
            unpack=True,
        )

    def oauth_login(self, url, oauth_token, oauth_token_secret,
                    consumer_key='anonymous', consumer_secret='anonymous'):
        """Authenticate using the OAUTH method.

        This only works with IMAP servers that support OAUTH (e.g. Gmail).
        """
        if oauth_module:
            token = oauth_module.Token(oauth_token, oauth_token_secret)
            consumer = oauth_module.Consumer(consumer_key, consumer_secret)
            xoauth_callable = lambda x: oauth_module.build_xoauth_string(url, consumer, token)
            return self._command_and_check('authenticate', 'XOAUTH', xoauth_callable, unpack=True)
        else:
            raise self.Error('The optional oauth2 package is needed for OAUTH authentication')

    def oauth2_login(self, user, access_token, mech='XOAUTH2', vendor=None):
        """Authenticate using the OAUTH2 method.

        Gmail and Yahoo both support the 'XOAUTH2' mechanism, but Yahoo requires
        the 'vendor' portion in the payload.
        """
        auth_string = 'user=%s\1auth=Bearer %s\1' % (user, access_token)
        if vendor:
            auth_string += 'vendor=%s\1' % vendor
        auth_string += '\1'
        return self._command_and_check('authenticate', mech, lambda x: auth_string)

    def logout(self):
        """Logout, returning the server response.
        """
        typ, data = self._imap.logout()
        self._check_resp('BYE', 'logout', typ, data)
        return data[0]

    def id_(self, parameters=None):
        """Issue the ID command, returning a dict of server implementation
        fields.

        *parameters* should be specified as a dictionary of field/value pairs,
        for example: ``{"name": "IMAPClient", "version": "0.12"}``
        """
        if not self.has_capability('ID'):
            raise ValueError('server does not support IMAP ID extension')
        if parameters is None:
            args = 'NIL'
        else:
            if not isinstance(parameters, dict):
                raise TypeError("'parameters' should be a dictionary")
            args = seq_to_parenstr(
                _quote(v) for v in
                itertools.chain.from_iterable(parameters.items()))

        typ, data = self._imap._simple_command('ID', args)
        self._checkok('id', typ, data)
        typ, data = self._imap._untagged_response(typ, data, 'ID')
        return parse_response(data)

    def capabilities(self):
        """Returns the server capability list.

        If the session is authenticated and the server has returned a
        CAPABILITY response at authentication time, this response
        will be returned. Otherwise, the CAPABILITY command will be
        issued to the server, with the results cached for future calls.

        If the session is not yet authenticated, the cached
        capabilities determined at connection time will be returned.
        """
        # if a capability response has been cached, use that
        if self._cached_capabilities:
            return self._cached_capabilities

        # If server returned an untagged CAPABILITY response (during
        # authentication), cache it and return that.

        untagged = normalise_untagged_responses(self._imap.untagged_responses)
        response = untagged.pop(b'CAPABILITY', None)
        if response:
            return self._save_capabilities(response[0])

        # if authenticated, but don't have a capability reponse, ask for one
        if self._imap.state in ('SELECTED', 'AUTH'):
            response = self._command_and_check('capability', unpack=True)
            return self._save_capabilities(response)

        # Just return capabilities that imaplib grabbed at connection
        # time (pre-auth)
        return tuple(to_bytes(c) for c in self._imap.capabilities)

    def _save_capabilities(self, raw_response):
        raw_response = to_bytes(raw_response)
        self._cached_capabilities = tuple(raw_response.upper().split())
        return self._cached_capabilities

    def has_capability(self, capability):
        """Return ``True`` if the IMAP server has the given *capability*.
        """
        # FIXME: this will not detect capabilities that are backwards
        # compatible with the current level. For instance the SORT
        # capabilities may in the future be named SORT2 which is
        # still compatible with the current standard and will not
        # be detected by this method.
        return to_bytes(capability).upper() in self.capabilities()

    def namespace(self):
        """Return the namespace for the account as a (personal, other,
        shared) tuple.

        Each element may be None if no namespace of that type exists,
        or a sequence of (prefix, separator) pairs.

        For convenience the tuple elements may be accessed
        positionally or using attributes named *personal*, *other* and
        *shared*.

        See :rfc:`2342` for more details.
        """
        data = self._command_and_check('namespace')
        parts = []
        for item in parse_response(data):
            if item is None:
                parts.append(item)
            else:
                converted = []
                for prefix, separator in item:
                    if self.folder_encode:
                        prefix = decode_utf7(prefix)
                    converted.append((prefix, to_unicode(separator)))
                parts.append(tuple(converted))
        return Namespace(*parts)

    def list_folders(self, directory="", pattern="*"):
        """Get a listing of folders on the server as a list of
        ``(flags, delimiter, name)`` tuples.

        Specifying *directory* will limit returned folders to the
        given base directory. The directory and any child directories
        will returned.

        Specifying *pattern* will limit returned folders to those with
        matching names. The wildcards are supported in
        *pattern*. ``*`` matches zero or more of any character and
        ``%`` matches 0 or more characters except the folder
        delimiter.

        Calling list_folders with no arguments will recursively list
        all folders available for the logged in user.

        Folder names are always returned as unicode strings, and
        decoded from modified UTF-7, except if folder_decode is not
        set.
        """
        return self._do_list(b'LIST', directory, pattern)

    def xlist_folders(self, directory="", pattern="*"):
        """Execute the XLIST command, returning ``(flags, delimiter,
        name)`` tuples.

        This method returns special flags for each folder and a
        localized name for certain folders (e.g. the name of the
        inbox may be localized and the flags can be used to
        determine the actual inbox, even if the name has been
        localized.

        A ``XLIST`` response could look something like::

            [((b'\\HasNoChildren', b'\\Inbox'), b'/', u'Inbox'),
             ((b'\\Noselect', b'\\HasChildren'), b'/', u'[Gmail]'),
             ((b'\\HasNoChildren', b'\\AllMail'), b'/', u'[Gmail]/All Mail'),
             ((b'\\HasNoChildren', b'\\Drafts'), b'/', u'[Gmail]/Drafts'),
             ((b'\\HasNoChildren', b'\\Important'), b'/', u'[Gmail]/Important'),
             ((b'\\HasNoChildren', b'\\Sent'), b'/', u'[Gmail]/Sent Mail'),
             ((b'\\HasNoChildren', b'\\Spam'), b'/', u'[Gmail]/Spam'),
             ((b'\\HasNoChildren', b'\\Starred'), b'/', u'[Gmail]/Starred'),
             ((b'\\HasNoChildren', b'\\Trash'), b'/', u'[Gmail]/Trash')]

        This is a *deprecated* Gmail-specific IMAP extension (See
        https://developers.google.com/gmail/imap_extensions#xlist_is_deprecated
        for more information). It is the responsibility of the caller
        to either check for ``XLIST`` in the server capabilites, or to
        handle the error if the server doesn't support this extension.

        The *directory* and *pattern* arguments are as per
        list_folders().
        """
        return self._do_list(b'XLIST', directory, pattern)

    def list_sub_folders(self, directory="", pattern="*"):
        """Return a list of subscribed folders on the server as
        ``(flags, delimiter, name)`` tuples.

        The default behaviour will list all subscribed folders. The
        *directory* and *pattern* arguments are as per list_folders().
        """
        return self._do_list(b'LSUB', directory, pattern)

    def _do_list(self, cmd, directory, pattern):
        directory = self._normalise_folder(directory)
        pattern = self._normalise_folder(pattern)
        typ, dat = self._imap._simple_command(cmd, directory, pattern)
        self._checkok(cmd, typ, dat)
        typ, dat = self._imap._untagged_response(typ, dat, cmd)
        return self._proc_folder_list(dat)

    def _proc_folder_list(self, folder_data):
        # Filter out empty strings and None's.
        # This also deals with the special case of - no 'untagged'
        # responses (ie, no folders). This comes back as [None].
        folder_data = [item for item in folder_data if item not in ('', None)]

        ret = []
        parsed = parse_response(folder_data)
        while parsed:
            # TODO: could be more efficient
            flags, delim, name = parsed[:3]
            parsed = parsed[3:]

            if isinstance(name, int):
                # Some IMAP implementations return integer folder names
                # with quotes. These get parsed to ints so convert them
                # back to strings.
                name = text_type(name)
            elif self.folder_encode:
                name = decode_utf7(name)

            ret.append((flags, delim, name))
        return ret

    def select_folder(self, folder, readonly=False):
        """Set the current folder on the server.

        Future calls to methods such as search and fetch will act on
        the selected folder.

        Returns a dictionary containing the ``SELECT`` response. At least
        the ``b'EXISTS'``, ``b'FLAGS'`` and ``b'RECENT'`` keys are guaranteed
        to exist. An example::

            {b'EXISTS': 3,
             b'FLAGS': (b'\\Answered', b'\\Flagged', b'\\Deleted', ... ),
             b'RECENT': 0,
             b'PERMANENTFLAGS': (b'\\Answered', b'\\Flagged', b'\\Deleted', ... ),
             b'READ-WRITE': True,
             b'UIDNEXT': 11,
             b'UIDVALIDITY': 1239278212}
        """
        self._command_and_check('select', self._normalise_folder(folder), readonly)
        return self._process_select_response(self._imap.untagged_responses)

    def _process_select_response(self, resp):
        untagged = normalise_untagged_responses(resp)
        out = {}

        # imaplib doesn't parse these correctly (broken regex) so replace
        # with the raw values out of the OK section
        for line in untagged.get(b'OK', []):
            match = re.match(br'\[(?P<key>[A-Z-]+)( \((?P<data>.*)\))?\]', line)
            if match:
                key = match.group('key')
                if key == b'PERMANENTFLAGS':
                    out[key] = tuple(match.group('data').split())

        for key, value in iteritems(untagged):
            key = key.upper()
            if key in (b'OK', b'PERMANENTFLAGS'):
                continue  # already handled above
            elif key in (b'EXISTS', b'RECENT', b'UIDNEXT', b'UIDVALIDITY', b'HIGHESTMODSEQ'):
                value = int(value[0])
            elif key == b'READ-WRITE':
                value = True
            elif key == b'FLAGS':
                value = tuple(value[0][1:-1].split())
            out[key] = value
        return out

    def noop(self):
        """Execute the NOOP command.

        This command returns immediately, returning any server side
        status updates. It can also be used to reset any auto-logout
        timers.

        The return value is the server command response message
        followed by a list of status responses. For example::

            (b'NOOP completed.',
             [(4, b'EXISTS'),
              (3, b'FETCH', (b'FLAGS', (b'bar', b'sne'))),
              (6, b'FETCH', (b'FLAGS', (b'sne',)))])

        """
        tag = self._imap._command('NOOP')
        return self._consume_until_tagged_response(tag, 'NOOP')

    def idle(self):
        """Put the server into IDLE mode.

        In this mode the server will return unsolicited responses
        about changes to the selected mailbox. This method returns
        immediately. Use ``idle_check()`` to look for IDLE responses
        and ``idle_done()`` to stop IDLE mode.

        .. note::

            Any other commmands issued while the server is in IDLE
            mode will fail.

        See :rfc:`2177` for more information about the IDLE extension.
        """
        self._idle_tag = self._imap._command('IDLE')
        resp = self._imap._get_response()
        if resp is not None:
            raise self.Error('Unexpected IDLE response: %s' % resp)

    def idle_check(self, timeout=None):
        """Check for any IDLE responses sent by the server.

        This method should only be called if the server is in IDLE
        mode (see ``idle()``).

        By default, this method will block until an IDLE response is
        received. If *timeout* is provided, the call will block for at
        most this many seconds while waiting for an IDLE response.

        The return value is a list of received IDLE responses. These
        will be parsed with values converted to appropriate types. For
        example::

            [(b'OK', b'Still here'),
             (1, b'EXISTS'),
             (1, b'FETCH', (b'FLAGS', (b'\\NotJunk',)))]
        """
        # In py2, imaplib has sslobj (for SSL connections), and sock for non-SSL.
        # In the py3 version it's just sock.
        sock = getattr(self._imap, 'sslobj', self._imap.sock)

        # make the socket non-blocking so the timeout can be
        # implemented for this call
        sock.setblocking(0)
        try:
            resps = []
            rs, _, _ = select.select([sock], [], [], timeout)
            if rs:
                while True:
                    try:
                        line = self._imap._get_line()
                    except (socket.timeout, socket.error):
                        break
                    except IMAPClient.AbortError:
                        # An imaplib.IMAP4.abort with "EOF" is raised
                        # under Python 3
                        err = sys.exc_info()[1]
                        if 'EOF' in err.args[0]:
                            break
                        else:
                            raise
                    else:
                        resps.append(_parse_untagged_response(line))
            return resps
        finally:
            sock.setblocking(1)

    def idle_done(self):
        """Take the server out of IDLE mode.

        This method should only be called if the server is already in
        IDLE mode.

        The return value is of the form ``(command_text,
        idle_responses)`` where *command_text* is the text sent by the
        server when the IDLE command finished (eg. ``b'Idle
        terminated'``) and *idle_responses* is a list of parsed idle
        responses received since the last call to ``idle_check()`` (if
        any). These are returned in parsed form as per
        ``idle_check()``.
        """
        self._imap.send(b'DONE\r\n')
        return self._consume_until_tagged_response(self._idle_tag, 'IDLE')

    def folder_status(self, folder, what=None):
        """Return the status of *folder*.

        *what* should be a sequence of status items to query. This
        defaults to ``('MESSAGES', 'RECENT', 'UIDNEXT', 'UIDVALIDITY',
        'UNSEEN')``.

        Returns a dictionary of the status items for the folder with
        keys matching *what*.
        """
        if what is None:
            what = ('MESSAGES', 'RECENT', 'UIDNEXT', 'UIDVALIDITY', 'UNSEEN')
        else:
            what = normalise_text_list(what)
        what_ = '(%s)' % (' '.join(what))

        data = self._command_and_check('status', self._normalise_folder(folder), what_, unpack=True)
        _, status_items = parse_response([data])
        return dict(as_pairs(status_items))

    def close_folder(self):
        """Close the currently selected folder, returning the server
        response string.
        """
        return self._command_and_check('close', unpack=True)

    def create_folder(self, folder):
        """Create *folder* on the server returning the server response string.
        """
        return self._command_and_check('create', self._normalise_folder(folder), unpack=True)

    def rename_folder(self, old_name, new_name):
        """Change the name of a folder on the server.
        """
        return self._command_and_check('rename',
                                       self._normalise_folder(old_name),
                                       self._normalise_folder(new_name),
                                       unpack=True)

    def delete_folder(self, folder):
        """Delete *folder* on the server returning the server response string.
        """
        return self._command_and_check('delete', self._normalise_folder(folder), unpack=True)

    def folder_exists(self, folder):
        """Return ``True`` if *folder* exists on the server.
        """
        data = self._command_and_check('list', '""', self._normalise_folder(folder))
        data = [x for x in data if x]
        return len(data) == 1 and data[0] != None

    def subscribe_folder(self, folder):
        """Subscribe to *folder*, returning the server response string.
        """
        return self._command_and_check('subscribe', self._normalise_folder(folder))

    def unsubscribe_folder(self, folder):
        """Unsubscribe to *folder*, returning the server response string.
        """
        return self._command_and_check('unsubscribe', self._normalise_folder(folder))

    def search(self, criteria='ALL', charset=None):
        """Return a list of messages ids matching *criteria*.

        *criteria* should be a list of of one or more criteria
        specifications or a single critera string. Example values
        include::

             'NOT DELETED'
             'UNSEEN'
             'SINCE 1-Feb-2011'

        *charset* specifies the character set of the strings in the
        criteria. It defaults to US-ASCII.

        See :rfc:`3501#section-6.4.4` for more details.

        The returned list of message ids will have its *modseq*
        attribute set if the server appended a MODSEQ value to the
        search response (i.e. if a MODSEQ criteria was included in the
        search).
        """
        return self._search(normalise_search_criteria(criteria), charset)

    def gmail_search(self, query, charset=None):
        """Search using Gmail's X-GM-RAW attribute.

        *query* should be a valid Gmail search query string. For
        example: ``has:attachment in:unread``

        This method only works for IMAP servers that support X-GM-RAW,
        which is only likely to be Gmail.

        See https://developers.google.com/gmail/imap_extensions#extension_of_the_search_command_x-gm-raw
        for more info.

        *charset* specifies the character set used to encode the
        search string. It defaults to US-ASCII.
        """
        # the the query is sent as a literal to allow for 7-bit query strings
        if isinstance(query, string_types):
            query = query.encode(charset or 'us-ascii')
        self._imap.literal = query
        return self._search([b'X-GM-RAW'], charset)

    def _search(self, criteria, charset):
        if self.use_uid:
            args = []
            if charset:
                args.extend([b'CHARSET', charset])
            args.extend(criteria)
            typ, data = self._imap.uid(b'SEARCH', *args)
        else:
            typ, data = self._imap.search(charset, *criteria)

        self._checkok('search', typ, data)

        ids = SearchIds()
        for item in parse_response(data):
            if isinstance(item, int):
                ids.append(item)
            elif isinstance(item, tuple) and len(item) == 2 and item[0].lower() == b'modseq':
                ids.modseq = item[1]
        return ids

    def thread(self, algorithm='REFERENCES', criteria='ALL', charset='UTF-8'):
        """Return a list of messages threads matching *criteria*.

        Each thread is a list of messages ids.

        See :rfc:`5256` for more details.
        """
        algorithm = to_bytes(algorithm)
        if not self.has_capability(b'THREAD=' + algorithm):
            raise ValueError('server does not support %s threading algorithm'
                             % algorithm)

        if not criteria:
            raise ValueError('no criteria specified')

        args = [algorithm]
        if charset:
            args.append(to_bytes(charset))
        args.extend(normalise_search_criteria(criteria))

        data = self._command_and_check('thread', *args, uid=True)
        return parse_response(data)

    def sort(self, sort_criteria, criteria='ALL', charset='UTF-8'):
        """Return a list of message ids sorted by *sort_criteria* and
        optionally filtered by *criteria*.

        Example values for *sort_criteria* include::

            ARRIVAL
            REVERSE SIZE
            SUBJECT

        The *criteria* argument is as per search().

        See :rfc:`5256` for full details.

        Note that SORT is an extension to the IMAP4 standard so it may
        not be supported by all IMAP servers.
        """
        if not criteria:
            raise ValueError('no criteria specified')

        if not self.has_capability('SORT'):
            raise self.Error('The server does not support the SORT extension')

        ids = self._command_and_check('sort',
                                      seq_to_parenstr_upper(sort_criteria),
                                      charset,
                                      *normalise_search_criteria(criteria),
                                      uid=True, unpack=True)
        return [long(i) for i in ids.split()]

    def get_flags(self, messages):
        """Return the flags set for each message in *messages*.

        The return value is a dictionary structured like this: ``{
        msgid1: (flag1, flag2, ... ), }``.
        """
        response = self.fetch(messages, ['FLAGS'])
        return self._filter_fetch_dict(response, b'FLAGS')

    def add_flags(self, messages, flags):
        """Add *flags* to *messages*.

        *flags* should be a sequence of strings.

        Returns the flags set for each modified message (see
        *get_flags*).
        """
        return self._store(b'+FLAGS', messages, flags, b'FLAGS')

    def remove_flags(self, messages, flags):
        """Remove one or more *flags* from *messages*.

        *flags* should be a sequence of strings.

        Returns the flags set for each modified message (see
        *get_flags*).
        """
        return self._store(b'-FLAGS', messages, flags, b'FLAGS')

    def set_flags(self, messages, flags):
        """Set the *flags* for *messages*.

        *flags* should be a sequence of strings.

        Returns the flags set for each modified message (see
        *get_flags*).
        """
        return self._store(b'FLAGS', messages, flags, b'FLAGS')

    def get_gmail_labels(self, messages):
        """Return the label set for each message in *messages*.

        The return value is a dictionary structured like this: ``{
        msgid1: (label1, label2, ... ), }``.

        This only works with IMAP servers that support the X-GM-LABELS
        attribute (eg. Gmail).
        """
        response = self.fetch(messages, [b'X-GM-LABELS'])
        return self._filter_fetch_dict(response, b'X-GM-LABELS')

    def add_gmail_labels(self, messages, labels):
        """Add *labels* to *messages*.

        *labels* should be a sequence of strings.

        Returns the label set for each modified message (see
        *get_gmail_labels*).

        This only works with IMAP servers that support the X-GM-LABELS
        attribute (eg. Gmail).
        """
        return self._store(b'+X-GM-LABELS', messages, labels, b'X-GM-LABELS')

    def remove_gmail_labels(self, messages, labels):
        """Remove one or more *labels* from *messages*.

        *labels* should be a sequence of strings.

        Returns the label set for each modified message (see
        *get_gmail_labels*).

        This only works with IMAP servers that support the X-GM-LABELS
        attribute (eg. Gmail).
        """
        return self._store(b'-X-GM-LABELS', messages, labels, b'X-GM-LABELS')

    def set_gmail_labels(self, messages, labels):
        """Set the *labels* for *messages*.

        *labels* should be a sequence of strings.

        Returns the label set for each modified message (see
        *get_gmail_labels*).

        This only works with IMAP servers that support the X-GM-LABELS
        attribute (eg. Gmail).
        """
        return self._store(b'X-GM-LABELS', messages, labels, b'X-GM-LABELS')

    def delete_messages(self, messages):
        """Delete one or more *messages* from the currently selected
        folder.

        Returns the flags set for each modified message (see
        *get_flags*).
        """
        return self.add_flags(messages, DELETED)

    def fetch(self, messages, data, modifiers=None):
        """Retrieve selected *data* associated with one or more *messages*.

        *data* should be specified as a sequnce of strings, one item
        per data selector, for example ``['INTERNALDATE',
        'RFC822']``.

        *modifiers* are required for some extensions to the IMAP
        protocol (eg. :rfc:`4551`). These should be a sequnce of strings
        if specified, for example ``['CHANGEDSINCE 123']``.

        A dictionary is returned, indexed by message number. Each item
        in this dictionary is also a dictionary, with an entry
        corresponding to each item in *data*. Returned values will be
        appropriately typed. For example, integer values will be returned as
        Python integers, timestamps will be returned as datetime
        instances and ENVELOPE responses will be returned as
        :py:class:`Envelope <imapclient.response_types.Envelope>` instances.

        String data will generally be returned as bytes (Python 3) or
        str (Python 2).

        In addition to an element for each *data* item, the dict
        returned for each message also contains a *SEQ* key containing
        the sequence number for the message. This allows for mapping
        between the UID and sequence number (when the *use_uid*
        property is ``True``).

        Example::

            >> c.fetch([3293, 3230], ['INTERNALDATE', 'FLAGS'])
            {3230: {b'FLAGS': (b'\\Seen',),
                    b'INTERNALDATE': datetime.datetime(2011, 1, 30, 13, 32, 9),
                    b'SEQ': 84},
             3293: {b'FLAGS': (),
                    b'INTERNALDATE': datetime.datetime(2011, 2, 24, 19, 30, 36),
                    b'SEQ': 110}}

        """
        if not messages:
            return {}

        args = [
            'FETCH',
            join_message_ids(messages),
            seq_to_parenstr_upper(data),
            seq_to_parenstr_upper(modifiers) if modifiers else None
        ]
        if self.use_uid:
            args.insert(0, 'UID')
        tag = self._imap._command(*args)
        typ, data = self._imap._command_complete('FETCH', tag)
        self._checkok('fetch', typ, data)
        typ, data = self._imap._untagged_response(typ, data, 'FETCH')
        return parse_fetch_response(data, self.normalise_times, self.use_uid)

    def append(self, folder, msg, flags=(), msg_time=None):
        """Append a message to *folder*.

        *msg* should be a string contains the full message including
        headers.

        *flags* should be a sequence of message flags to set. If not
        specified no flags will be set.

        *msg_time* is an optional datetime instance specifying the
        date and time to set on the message. The server will set a
        time if it isn't specified. If *msg_time* contains timezone
        information (tzinfo), this will be honoured. Otherwise the
        local machine's time zone sent to the server.

        Returns the APPEND response as returned by the server.
        """
        if msg_time:
            time_val = '"%s"' % datetime_to_INTERNALDATE(msg_time)
            if PY3:
                time_val = to_unicode(time_val)
            else:
                time_val = to_bytes(time_val)
        else:
            time_val = None
        return self._command_and_check('append',
                                       self._normalise_folder(folder),
                                       seq_to_parenstr(flags),
                                       time_val,
                                       to_bytes(msg),
                                       unpack=True)

    def copy(self, messages, folder):
        """Copy one or more messages from the current folder to
        *folder*. Returns the COPY response string returned by the
        server.
        """
        return self._command_and_check('copy',
                                       join_message_ids(messages),
                                       self._normalise_folder(folder),
                                       uid=True, unpack=True)

    def expunge(self):
        """Remove any messages from the currently selected folder that
        have the ``\\Deleted`` flag set.

        The return value is the server response message
        followed by a list of expunge responses. For example::

            ('Expunge completed.',
             [(2, 'EXPUNGE'),
              (1, 'EXPUNGE'),
              (0, 'RECENT')])

        In this case, the responses indicate that the message with
        sequence numbers 2 and 1 where deleted, leaving no recent
        messages in the folder.

        See :rfc:`3501#section-6.4.3` section 6.4.3 and
        :rfc:`3501#section-7.4.1` section 7.4.1 for more details.
        """
        tag = self._imap._command('EXPUNGE')
        return self._consume_until_tagged_response(tag, 'EXPUNGE')

    def getacl(self, folder):
        """Returns a list of ``(who, acl)`` tuples describing the
        access controls for *folder*.
        """
        data = self._command_and_check('getacl', self._normalise_folder(folder))
        parts = list(response_lexer.TokenSource(data))
        parts = parts[1:]       # First item is folder name
        return [(parts[i], parts[i+1]) for i in xrange(0, len(parts), 2)]

    def setacl(self, folder, who, what):
        """Set an ACL (*what*) for user (*who*) for a folder.

        Set *what* to an empty string to remove an ACL. Returns the
        server response string.
        """
        return self._command_and_check('setacl',
                                       self._normalise_folder(folder),
                                       who, what,
                                       unpack=True)

    def _check_resp(self, expected, command, typ, data):
        """Check command responses for errors.

        Raises IMAPClient.Error if the command fails.
        """
        if typ != expected:
            raise self.Error('%s failed: %r' % (command, data[0]))

    def _consume_until_tagged_response(self, tag, command):
        tagged_commands = self._imap.tagged_commands
        resps = []
        while True:
            line = self._imap._get_response()
            if tagged_commands[tag]:
                break
            resps.append(_parse_untagged_response(line))
        typ, data = tagged_commands.pop(tag)
        self._checkok(command, typ, data)
        return data[0], resps

    def _command_and_check(self, command, *args, **kwargs):
        unpack = pop_with_default(kwargs, 'unpack', False)
        uid = pop_with_default(kwargs, 'uid', False)
        assert not kwargs, "unexpected keyword args: " + ', '.join(kwargs)

        if uid and self.use_uid:
            typ, data = self._imap.uid(command, *args)
        else:
            meth = getattr(self._imap, command)
            typ, data = meth(*args)
        self._checkok(command, typ, data)
        if unpack:
            return data[0]
        return data

    def _checkok(self, command, typ, data):
        self._check_resp('OK', command, typ, data)

    def _store(self, cmd, messages, flags, fetch_key):
        """Worker function for the various flag manipulation methods.

        *cmd* is the STORE command to use (eg. '+FLAGS').
        """
        if not messages:
            return {}
        data = self._command_and_check('store',
                                       join_message_ids(messages),
                                       cmd,
                                       seq_to_parenstr(flags),
                                       uid=True)
        return self._filter_fetch_dict(parse_fetch_response(data), fetch_key)

    def _filter_fetch_dict(self, fetch_dict, key):
        return dict((msgid, data[key])
                    for msgid, data in iteritems(fetch_dict))

    def __debug_get(self):
        return self._imap.debug

    def __debug_set(self, level):
        if level is True:
            level = 4
        elif level is False:
            level = 0
        self._imap.debug = level

    debug = property(__debug_get, __debug_set)

    def _log(self, text):
        self.log_file.write('%s %s\n' % (datetime.now().strftime('%M:%S.%f'), text))
        self.log_file.flush()

    def _normalise_folder(self, folder_name):
        if isinstance(folder_name, binary_type):
            folder_name = folder_name.decode('ascii')
        if self.folder_encode:
            folder_name = encode_utf7(folder_name)
        return _quote(folder_name)

def _quote(arg):
    if isinstance(arg, text_type):
        arg = arg.replace('\\', '\\\\')
        arg = arg.replace('"', '\\"')
        q = '"'
    else:
        arg = arg.replace(b'\\', b'\\\\')
        arg = arg.replace(b'"', b'\\"')
        q = b'"'
    return q + arg + q

# normalise_text_list, seq_to_parentstr etc have to return unicode
# because imaplib handles flags and sort criteria assuming these are
# passed as unicode
def normalise_text_list(items):
    return list(_normalise_text_list(items))

def seq_to_parenstr(items):
    return _join_and_paren(_normalise_text_list(items))

def seq_to_parenstr_upper(items):
    return _join_and_paren(item.upper() for item in _normalise_text_list(items))

def normalise_search_criteria(criteria):
    if not criteria:
        raise ValueError('no criteria specified')
    return ['(' + item + ')' for item in _normalise_text_list(criteria)]

def _join_and_paren(items):
    return '(' + ' '.join(items) + ')'

def _normalise_text_list(items):
    if isinstance(items, (text_type, binary_type)):
        items = (to_unicode(items),)
    return (to_unicode(c) for c in items)

def join_message_ids(messages):
    """Convert a sequence of messages ids or a single integer message id
    into an id byte string for use with IMAP commands
    """
    if isinstance(messages, (text_type, binary_type, integer_types)):
        messages = (to_bytes(messages),)
    return b','.join(_maybe_int_to_bytes(m) for m in messages)

def _maybe_int_to_bytes(val):
    if isinstance(val, integer_types):
        return str(val).encode('us-ascii')
    return to_bytes(val)

def normalise_untagged_responses(untagged):
    out = {}
    for key, value in iteritems(untagged):
        out[to_bytes(key)] = value
    return out

def _parse_untagged_response(text):
    assert text.startswith(b'* ')
    text = text[2:]
    if text.startswith((b'OK ', b'NO ')):
        return tuple(text.split(b' ', 1))
    return parse_response([text])

def pop_with_default(dct, key, default):
    if key in dct:
        return dct.pop(key)
    return default

def as_pairs(items):
    i = 0
    last_item = None
    for item in items:
        if i % 2:
            yield last_item, item
        else:
            last_item = item
        i += 1

def to_unicode(s):
    if isinstance(s, binary_type):
        return s.decode('ascii')
    return s

def to_bytes(s):
    if isinstance(s, text_type):
        return s.encode('ascii')
    return s
