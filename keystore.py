"""Where a private key lives while the process runs. Never where it is printed.

Two stores, both local:

    memory  the key exists for the lifetime of this process and nowhere else
    file    one key per file, mode 0600, never overwritten

The single rule this module enforces for the whole package: a key goes
IN and never comes back OUT through anything a caller can serialise.
`load`/`get` hand a key to code in this process; nothing here returns a
dict, and `redact` exists so callers can assert that.
"""

import errno
import os
import stat

import ed25519_blake2b as _ed
import nanoaddr as _addr


class KeyStoreError(Exception):
    def __init__(self, reason: str, message: str, **extra):
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.extra = extra


UNWRITABLE_HINT = (
    "Pass \"path\" as an absolute path in a directory this process can write; "
    "the key is created there at mode 600 and never overwritten. No key file was "
    "left behind and no address was issued."
)


def _unwritable(step: str, path: str, exc: OSError) -> KeyStoreError:
    """One error that names the step and the path, for a sandbox that said no."""
    shown = path if os.path.isabs(path) else "%s (%s)" % (path, os.path.abspath(path))
    return KeyStoreError(
        "key_path_unwritable",
        "create_address step %s failed for the key file at %s: %s. %s"
        % (step, shown, exc.strerror or exc, UNWRITABLE_HINT),
        step=step, path=path,
    )


def _make_private_directories(directory: str) -> None:
    """Create missing parents at 0700. os.makedirs(mode=) only sets the last one."""
    if os.path.isdir(directory):
        return
    if os.path.lexists(directory):
        raise NotADirectoryError(errno.ENOTDIR, "%s is not a directory" % directory)
    parent = os.path.dirname(directory)
    if parent and parent != directory:
        _make_private_directories(parent)
    try:
        os.mkdir(directory, 0o700)
    except FileExistsError:
        if not os.path.isdir(directory):
            raise NotADirectoryError(errno.ENOTDIR, "%s is not a directory" % directory) from None


class KeyStore:
    def __init__(self):
        self._keys = {}          # address -> private key bytes

    # -- in-process -------------------------------------------------------

    def put(self, private_key: bytes) -> str:
        public_key = _ed.public_key_from_private(private_key)
        address = _addr.encode(public_key)
        self._keys[address] = private_key
        return address

    def get(self, address: str) -> bytes:
        try:
            return self._keys[address]
        except KeyError:
            raise KeyStoreError(
                "key_not_loaded",
                "no private key for %s is loaded in this process. Create the address "
                "with create_address (store: \"file\") and load it with "
                "NANO_WALLET_KEY_FILE, or create it again in this session." % address,
            ) from None

    def has(self, address: str) -> bool:
        return address in self._keys

    def addresses(self) -> list:
        return sorted(self._keys)

    # -- files ------------------------------------------------------------

    def save(self, private_key: bytes, path: str) -> str:
        """Write a key at mode 0600, refusing to overwrite or to leave it readable."""
        path = os.path.expanduser(path)
        if os.path.exists(path):
            raise KeyStoreError(
                "key_exists",
                "refusing to overwrite an existing key file at %s - a key file that is "
                "overwritten is funds that are gone" % path,
            )
        directory = os.path.dirname(path) or "."
        try:
            _make_private_directories(directory)
        except OSError as exc:
            raise _unwritable("make_directory", path, exc) from None
        try:
            # 0600 and O_EXCL in the one call that creates it: there is no moment
            # when the file exists with a wider mode, and no race onto a file
            # that appeared after the exists() check above.
            handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            raise KeyStoreError(
                "key_exists",
                "refusing to overwrite an existing key file at %s - a key file that is "
                "overwritten is funds that are gone" % path,
            ) from None
        except OSError as exc:
            raise _unwritable("create_key_file", path, exc) from None
        data = (private_key.hex().upper() + "\n").encode("ascii")
        try:
            try:
                while data:
                    data = data[os.write(handle, data):]
                os.fsync(handle)
            finally:
                os.close(handle)
        except OSError as exc:
            # A zero-length or half-written key file looks like a key and is not.
            os.unlink(path)
            raise _unwritable("write_key_file", path, exc) from None
        mode = stat.S_IMODE(os.stat(path).st_mode)
        if mode & 0o077:
            # A umask or a filesystem that ignores the mode would leave the key
            # readable by other users. Delete it rather than report success.
            os.unlink(path)
            raise KeyStoreError(
                "key_permissions",
                "the key file at %s came back mode %o instead of 600, so it was deleted "
                "unused. This filesystem cannot hold a private key safely." % (path, mode),
            )
        return path

    def load_file(self, path: str) -> str:
        path = os.path.expanduser(path)
        try:
            with open(path, "r", encoding="ascii") as handle:
                text = handle.read().strip()
        except OSError as exc:
            raise KeyStoreError("key_unreadable",
                                "cannot read the key file at %s: %s" % (path, exc.strerror)) from None
        try:
            private_key = bytes.fromhex(text)
        except ValueError:
            raise KeyStoreError("key_malformed",
                                "the key file at %s is not 64 hex characters" % path) from None
        if len(private_key) != 32:
            raise KeyStoreError("key_malformed",
                                "the key file at %s holds %d bytes, expected 32"
                                % (path, len(private_key)))
        return self.put(private_key)

    def redact(self, address: str, text: str) -> str:
        """Replace a loaded key with a marker. Used only to prove absence in tests."""
        key = self._keys.get(address)
        return text if key is None else text.replace(key.hex().upper(), "<redacted>")
