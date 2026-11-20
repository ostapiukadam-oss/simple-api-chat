import ctypes
import os

if os.name == "nt":
    import ctypes.wintypes as wintypes

    class DataBlob(ctypes.Structure):
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_byte)),
        ]


def _dpapi_functions():
    if os.name != "nt":
        raise OSError("Systemowy magazyn kluczy jest dostępny tylko w Windows.")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(DataBlob),
        wintypes.LPCWSTR,
        ctypes.POINTER(DataBlob),
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(DataBlob),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(DataBlob),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(DataBlob),
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(DataBlob),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL
    return crypt32, kernel32


def protect_api_key(value):
    """Encrypt an API key with Windows DPAPI for the current user."""
    crypt32, kernel32 = _dpapi_functions()
    raw = value.encode("utf-8")
    source_buffer = ctypes.create_string_buffer(raw)
    source = DataBlob(len(raw), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_byte)))
    encrypted = DataBlob()
    if not crypt32.CryptProtectData(
        ctypes.byref(source), "MojCzatOpenAI", None, None, None, 0, ctypes.byref(encrypted)
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(encrypted.pbData, encrypted.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(encrypted.pbData, wintypes.HLOCAL))


def unprotect_api_key(data):
    crypt32, kernel32 = _dpapi_functions()
    source_buffer = ctypes.create_string_buffer(data)
    source = DataBlob(len(data), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_byte)))
    decrypted = DataBlob()
    if not crypt32.CryptUnprotectData(
        ctypes.byref(source), None, None, None, None, 0, ctypes.byref(decrypted)
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(decrypted.pbData, decrypted.cbData).decode("utf-8")
    finally:
        kernel32.LocalFree(ctypes.cast(decrypted.pbData, wintypes.HLOCAL))
