"""Generate tiny synthetic images in temporary directories; no personal photos."""
import struct
import zlib


def png(path, color=0):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
    data = b"\x89PNG\r\n\x1a\n"
    data += chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    data += chunk(b"IDAT", zlib.compress(bytes([0, color, color, color])))
    data += chunk(b"IEND", b"")
    path.write_bytes(data)
    return path


def jpeg(path):
    def segment(marker, data):
        return b"\xff" + bytes([marker]) + struct.pack(">H", len(data) + 2) + data
    # Baseline grayscale 8x8 block, DC=0 and EOB; one-bit Huffman codes.
    data = b"\xff\xd8"
    data += segment(0xdb, bytes([0]) + bytes([1]) * 64)
    data += segment(0xc0, bytes([8]) + struct.pack(">HH", 8, 8) + bytes([1, 1, 0x11, 0]))
    data += segment(0xc4, bytes([0, 1]) + bytes(15) + bytes([0, 0x10, 1]) + bytes(15) + bytes([0]))
    data += segment(0xda, bytes([1, 1, 0, 0, 63, 0]))
    path.write_bytes(data + b"\x3f\xff\xd9")
    return path


def heic(path, tiff):
    """Minimal HEIF metadata container, intentionally no decodable image payload."""
    def box(kind, data):
        return struct.pack(">I", len(data) + 8) + kind + data
    ftyp = box(b"ftyp", b"heic" + bytes(4) + b"mif1heic")
    exif = bytes(4) + tiff
    infe = box(b"infe", b"\x02\0\0\0" + struct.pack(">HH", 1, 0) + b"ExifExif\0")
    iinf = box(b"iinf", bytes(4) + struct.pack(">H", 1) + infe)

    def meta(offset):
        iloc = box(b"iloc", bytes(4) + b"\x44\0" + struct.pack(">HHHHII", 1, 1, 0, 1, offset, len(exif)))
        return box(b"meta", bytes(4) + iinf + iloc)
    path.write_bytes(ftyp + meta(len(ftyp) + len(meta(0)) + 8) + box(b"mdat", exif))
    return path
