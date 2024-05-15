# Copyright (c) 2001-2024 Python Software Foundation; All Rights Reserved
import threading
import struct
import io
import os
import sys

try:
    import zlib

    crc32 = zlib.crc32
except ImportError:
    zlib = None
    crc32 = binascii.crc32

structCentralDir = "<4s4B4HL2L5H2L"
stringCentralDir = b"PK\001\002"
sizeCentralDir = struct.calcsize(structCentralDir)

structFileHeader = "<4s2B4HL2L2H"
stringFileHeader = b"PK\003\004"
sizeFileHeader = struct.calcsize(structFileHeader)

structEndArchive = b"<4s4H2LH"
stringEndArchive = b"PK\005\006"
sizeEndCentDir = struct.calcsize(structEndArchive)

structEndArchive64 = "<4sQ2H2L4Q"
stringEndArchive64 = b"PK\x06\x06"
sizeEndCentDir64 = struct.calcsize(structEndArchive64)

structEndArchive64Locator = "<4sLQL"
stringEndArchive64Locator = b"PK\x06\x07"
sizeEndCentDir64Locator = struct.calcsize(structEndArchive64Locator)


class BadZipFile(Exception):
    pass


def _sanitize_filename(filename):
    null_byte = filename.find(chr(0))
    if null_byte >= 0:
        filename = filename[0:null_byte]
    if os.sep != "/" and os.sep in filename:
        filename = filename.replace(os.sep, "/")
    if os.altsep and os.altsep != "/" and os.altsep in filename:
        filename = filename.replace(os.altsep, "/")
    return filename


def _EndRecData64(fpin, offset, endrec):
    try:
        fpin.seek(offset - sizeEndCentDir64Locator, 2)
    except OSError:
        return endrec

    data = fpin.read(sizeEndCentDir64Locator)
    if len(data) != sizeEndCentDir64Locator:
        return endrec
    sig, diskno, _, disks = struct.unpack(structEndArchive64Locator, data)
    if sig != stringEndArchive64Locator:
        return endrec

    if diskno != 0 or disks > 1:
        raise BadZipFile("zipfiles that span multiple disks are not supported")

    fpin.seek(offset - sizeEndCentDir64Locator - sizeEndCentDir64, 2)
    data = fpin.read(sizeEndCentDir64)
    if len(data) != sizeEndCentDir64:
        return endrec
    (
        sig,
        *_,
        disk_num,
        disk_dir,
        dircount,
        dircount2,
        dirsize,
        diroffset,
    ) = struct.unpack(structEndArchive64, data)
    if sig != stringEndArchive64:
        return endrec

    endrec[0] = sig
    endrec[1] = disk_num
    endrec[2] = disk_dir
    endrec[3] = dircount
    endrec[4] = dircount2
    endrec[5] = dirsize
    endrec[6] = diroffset
    return endrec


def _EndRecData(fpin):
    fpin.seek(0, 2)
    filesize = fpin.tell()
    try:
        fpin.seek(-sizeEndCentDir, 2)
    except OSError:
        return None
    data = fpin.read()
    if (
        len(data) == sizeEndCentDir
        and data[0:4] == stringEndArchive
        and data[-2:] == b"\000\000"
    ):
        endrec = struct.unpack(structEndArchive, data)
        endrec = list(endrec)

        endrec.append(b"")
        endrec.append(filesize - sizeEndCentDir)

        return _EndRecData64(fpin, -sizeEndCentDir, endrec)
    maxCommentStart = max(filesize - (1 << 16) - sizeEndCentDir, 0)
    fpin.seek(maxCommentStart, 0)
    data = fpin.read()
    start = data.rfind(stringEndArchive)
    if start >= 0:
        recData = data[start : start + sizeEndCentDir]
        if len(recData) != sizeEndCentDir:
            return None
        endrec = list(struct.unpack(structEndArchive, recData))
        commentSize = endrec[7]
        comment = data[start + sizeEndCentDir : start + sizeEndCentDir + commentSize]
        endrec.append(comment)
        endrec.append(maxCommentStart + start)

        return _EndRecData64(fpin, maxCommentStart + start - filesize, endrec)


class ZipExtFile(io.BufferedIOBase):
    MAX_N = 1 << 31 - 1
    MIN_READ_SIZE = 4096
    MAX_SEEK_READ = 1 << 24

    def __init__(self, fileobj, zipinfo):
        self._fileobj = fileobj

        self._compress_left = zipinfo.compress_size
        self._left = zipinfo.file_size

        self._eof = False
        self._readbuffer = b""
        self._offset = 0

        self.name = zipinfo.filename

        if hasattr(zipinfo, "CRC"):
            self._expected_crc = zipinfo.CRC
            self._running_crc = crc32(b"")
        else:
            self._expected_crc = None

        self._seekable = False
        try:
            if fileobj.seekable():
                self._orig_compress_start = fileobj.tell()
                self._orig_compress_size = zipinfo.compress_size
                self._orig_file_size = zipinfo.file_size
                self._orig_start_crc = self._running_crc
                self._orig_crc = self._expected_crc
                self._seekable = True
        except AttributeError:
            pass

        self._decrypter = None

    def read(self, n=-1):
        if self.closed:
            raise ValueError("read from closed file.")
        if n is None or n < 0:
            buf = self._readbuffer[self._offset :]
            self._readbuffer = b""
            self._offset = 0
            while not self._eof:
                buf += self._read1(self.MAX_N)
            return buf

        end = n + self._offset
        if end < len(self._readbuffer):
            buf = self._readbuffer[self._offset : end]
            self._offset = end
            return buf

        n = end - len(self._readbuffer)
        buf = self._readbuffer[self._offset :]
        self._readbuffer = b""
        self._offset = 0
        while n > 0 and not self._eof:
            data = self._read1(n)
            if n < len(data):
                self._readbuffer = data
                self._offset = n
                buf += data[:n]
                break
            buf += data
            n -= len(data)
        return buf

    def _read1(self, n):

        if self._eof or n <= 0:
            return b""

        data = self._read2(n)

        self._eof = self._compress_left <= 0

        data = data[: self._left]
        self._left -= len(data)
        if self._left <= 0:
            self._eof = True
        self._update_crc(data)
        return data

    def _read2(self, n):
        if self._compress_left <= 0:
            return b""

        n = max(n, self.MIN_READ_SIZE)
        n = min(n, self._compress_left)

        data = self._fileobj.read(n)
        self._compress_left -= len(data)
        if not data:
            raise EOFError

        if self._decrypter is not None:
            data = self._decrypter(data)
        return data

    def _update_crc(self, newdata):

        if self._expected_crc is None:

            return
        self._running_crc = crc32(newdata, self._running_crc)

        if self._eof and self._running_crc != self._expected_crc:
            raise BadZipFile("Bad CRC-32 for file %r" % self.name)

    def seek(self, offset, whence=os.SEEK_SET):
        if self.closed:
            raise ValueError("seek on closed file.")
        if not self._seekable:
            raise io.UnsupportedOperation("underlying stream is not seekable")
        curr_pos = self.tell()
        if whence == os.SEEK_SET:
            new_pos = offset
        elif whence == os.SEEK_CUR:
            new_pos = curr_pos + offset
        elif whence == os.SEEK_END:
            new_pos = self._orig_file_size + offset
        else:
            raise ValueError(
                "whence must be os.SEEK_SET (0), " "os.SEEK_CUR (1), or os.SEEK_END (2)"
            )

        if new_pos > self._orig_file_size:
            new_pos = self._orig_file_size

        if new_pos < 0:
            new_pos = 0

        read_offset = new_pos - curr_pos
        buff_offset = read_offset + self._offset

        if buff_offset >= 0 and buff_offset < len(self._readbuffer):

            self._offset = buff_offset
            read_offset = 0
        elif self._decrypter is None and read_offset > 0:

            self._expected_crc = None

            read_offset -= len(self._readbuffer) - self._offset
            self._fileobj.seek(read_offset, os.SEEK_CUR)
            self._left -= read_offset
            read_offset = 0

            self._readbuffer = b""
            self._offset = 0
        elif read_offset < 0:

            self._fileobj.seek(self._orig_compress_start)
            self._running_crc = self._orig_start_crc
            self._expected_crc = self._orig_crc
            self._compress_left = self._orig_compress_size
            self._left = self._orig_file_size
            self._readbuffer = b""
            self._offset = 0
            self._eof = False
            read_offset = new_pos
            if self._decrypter is not None:
                self._init_decrypter()

        while read_offset > 0:
            read_len = min(self.MAX_SEEK_READ, read_offset)
            self.read(read_len)
            read_offset -= read_len

        return self.tell()

    def tell(self):
        if self.closed:
            raise ValueError("tell on closed file.")
        if not self._seekable:
            raise io.UnsupportedOperation("underlying stream is not seekable")
        filepos = (
            self._orig_file_size - self._left - len(self._readbuffer) + self._offset
        )
        return filepos


class ZipInfo(object):
    """Class with attributes describing each file in the ZIP archive."""

    def __init__(self, filename="NoName"):
        self.orig_filename = filename
        filename = _sanitize_filename(filename)

        self.filename = filename
        self.comment = b""
        self.extra = b""
        self.extract_version = 20
        self.flag_bits = 0
        self.compress_size = 0
        self.file_size = 0
        self._end_offset = None

    def _decodeExtra(self, filename_crc):

        extra = self.extra
        unpack = struct.unpack
        while len(extra) >= 4:
            tp, ln = unpack("<HH", extra[:4])
            if ln + 4 > len(extra):
                raise BadZipFile("Corrupt extra field %04x (size=%d)" % (tp, ln))
            if tp == 1:
                data = extra[4 : ln + 4]

                try:
                    if self.file_size in (0xFFFF_FFFF_FFFF_FFFF, 0xFFFF_FFFF):
                        field = "File size"
                        (self.file_size,) = unpack("<Q", data[:8])
                        data = data[8:]
                    if self.compress_size == 0xFFFF_FFFF:
                        field = "Compress size"
                        (self.compress_size,) = unpack("<Q", data[:8])
                        data = data[8:]
                    if self.header_offset == 0xFFFF_FFFF:
                        field = "Header offset"
                        (self.header_offset,) = unpack("<Q", data[:8])
                except struct.error:
                    raise BadZipFile(
                        f"Corrupt zip64 extra field. " f"{field} not found."
                    ) from None
            elif tp == 0x7075:
                data = extra[4 : ln + 4]
                try:
                    up_version, up_name_crc = unpack("<BL", data[:5])
                    if up_version == 1 and up_name_crc == filename_crc:
                        up_unicode_name = data[5:].decode("utf-8")
                        if up_unicode_name:
                            self.filename = _sanitize_filename(up_unicode_name)
                        else:
                            import warnings

                            warnings.warn(
                                "Empty unicode path extra field (0x7075)", stacklevel=2
                            )
                except struct.error as e:
                    raise BadZipFile("Corrupt unicode path extra field (0x7075)") from e
                except UnicodeDecodeError as e:
                    raise BadZipFile(
                        "Corrupt unicode path extra field (0x7075): invalid utf-8 bytes"
                    ) from e

            extra = extra[ln + 4 :]


class _SharedFile:
    def __init__(self, file, pos, close, lock):
        self._file = file
        self._pos = pos
        self._close = close
        self._lock = lock
        self.seekable = file.seekable

    def tell(self):
        return self._pos

    def read(self, n=-1):
        with self._lock:
            self._file.seek(self._pos)
            data = self._file.read(n)
            self._pos = self._file.tell()
            return data

    def seek(self, offset, whence=0):
        with self._lock:
            self._file.seek(offset, whence)
            self._pos = self._file.tell()
            return self._pos

    def close(self):
        if self._file is not None:
            fileobj = self._file
            self._file = None
            self._close(fileobj)


class ZipFile:
    fp = None

    def __init__(
        self,
        file,
    ):

        self.NameToInfo = {}
        self.filelist = []

        self.fp = file
        self.filename = getattr(file, "name", None)
        self._fileRefCnt = 1
        self._lock = threading.RLock()
        self._seekable = True

        try:
            self._RealGetContents()
        except:
            self._fpclose(self.fp)
            raise

    def _RealGetContents(self):
        """Read in the table of contents for the ZIP file."""
        fp = self.fp
        try:
            endrec = _EndRecData(fp)
        except OSError:
            raise BadZipFile("File is not a zip file")
        if not endrec:
            raise BadZipFile("File is not a zip file")
        size_cd = endrec[5]
        offset_cd = endrec[6]

        concat = endrec[9] - size_cd - offset_cd
        if endrec[0] == stringEndArchive64:

            concat -= sizeEndCentDir64 + sizeEndCentDir64Locator

        self.start_dir = offset_cd + concat
        if self.start_dir < 0:
            raise BadZipFile("Bad offset for central directory")
        fp.seek(self.start_dir, 0)
        data = fp.read(size_cd)
        fp = io.BytesIO(data)
        total = 0
        while total < size_cd:
            centdir = fp.read(sizeCentralDir)
            if len(centdir) != sizeCentralDir:
                raise BadZipFile("Truncated central directory")
            centdir = struct.unpack(structCentralDir, centdir)
            if centdir[0] != stringCentralDir:
                raise BadZipFile("Bad magic number for central directory")
            filename = fp.read(centdir[12])
            orig_filename_crc = crc32(filename)
            flags = centdir[5]
            if flags & 0x0800:

                filename = filename.decode("utf-8")
            else:

                filename = filename.decode("cp437")

            x = ZipInfo(filename)
            x.extra = fp.read(centdir[13])
            x.comment = fp.read(centdir[14])
            x.header_offset = centdir[18]
            (
                x.extract_version,
                _,
                x.flag_bits,
                *_,
                x.CRC,
                x.compress_size,
                x.file_size,
            ) = centdir[3:12]
            if x.extract_version > 63:
                raise NotImplementedError(
                    "zip file version %.1f" % (x.extract_version / 10)
                )
            x._decodeExtra(orig_filename_crc)
            x.header_offset = x.header_offset + concat
            self.filelist.append(x)
            self.NameToInfo[x.filename] = x

            total = total + sizeCentralDir + centdir[12] + centdir[13] + centdir[14]

        end_offset = self.start_dir
        for zinfo in sorted(
            self.filelist, key=lambda zinfo: zinfo.header_offset, reverse=True
        ):
            zinfo._end_offset = end_offset
            end_offset = zinfo.header_offset

    def open(self, name):

        zinfo = self.NameToInfo.get(name)
        if zinfo is None:
            raise KeyError("There is no item named %r in the archive" % name)

        self._fileRefCnt += 1
        zef_file = _SharedFile(
            self.fp,
            zinfo.header_offset,
            self._fpclose,
            self._lock,
        )
        try:

            fheader = zef_file.read(sizeFileHeader)
            if len(fheader) != sizeFileHeader:
                raise BadZipFile("Truncated file header")
            fheader = struct.unpack(structFileHeader, fheader)
            if fheader[0] != stringFileHeader:
                raise BadZipFile("Bad magic number for file header")

            fname = zef_file.read(fheader[10])
            if fheader[11]:
                zef_file.seek(fheader[11], whence=1)

            if zinfo.flag_bits & 0x0020:

                raise NotImplementedError("compressed patched data (flag bit 5)")

            if zinfo.flag_bits & 0x0040:

                raise NotImplementedError("strong encryption (flag bit 6)")

            fname_str = fname.decode("utf-8" if fheader[3] & 0x0800 else "cp437")

            if fname_str != zinfo.orig_filename:
                raise BadZipFile(
                    "File name in directory %r and header %r differ."
                    % (zinfo.orig_filename, fname)
                )

            if (
                zinfo._end_offset is not None
                and zef_file.tell() + zinfo.compress_size > zinfo._end_offset
            ):
                raise BadZipFile(
                    f"Overlapped entries: {zinfo.orig_filename!r} (possible zip bomb)"
                )

            is_encrypted = zinfo.flag_bits & 1
            if is_encrypted:
                raise BadZipFile

            return ZipExtFile(zef_file, zinfo)
        except Exception as e:
            print(e)
            zef_file.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, type, value, traceback):
        self.close()

    def close(self):
        if self.fp is None:
            return
        self._fpclose(self.fp)

    def _fpclose(self, fp):
        fp = self.fp
        self.fp = None
        assert self._fileRefCnt > 0
        self._fileRefCnt -= 1
