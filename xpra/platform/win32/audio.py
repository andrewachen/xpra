# This file is part of Xpra.
# Copyright (C) 2026 Netflix, Inc.
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.
# ABOUTME: Win32 audio device change detection via WM_DEVICECHANGE.
# ABOUTME: Tracks the default WASAPI endpoint to filter non-audio device events.

from xpra.platform.win32.constants import WM_DEVICECHANGE
from xpra.platform.win32.events import get_win32_event_listener

from xpra.log import Logger

log = Logger("audio")

_last_endpoint_id: str = ""


_comtypes_ready: bool = False
_IMMDeviceEnumerator = None
_CLSID_MMDeviceEnumerator = None


def _init_mmdevice_com() -> bool:
    """Initialize COM and MMDeviceAPI interface definitions (once)."""
    global _comtypes_ready, _IMMDeviceEnumerator, _CLSID_MMDeviceEnumerator
    if _comtypes_ready:
        return True
    try:
        from xpra.platform.win32.comtypes_util import comtypes_init
        comtypes_init()
        import comtypes                         # pylint: disable=import-outside-toplevel
        from comtypes import COMMETHOD, HRESULT, GUID
        from ctypes import POINTER, c_uint, c_wchar_p, c_void_p

        class IMMDevice(comtypes.IUnknown):
            _iid_ = GUID('{D666063F-1587-4E43-81F1-B948E807363F}')
            _methods_ = [
                COMMETHOD([], HRESULT, 'Activate',
                          (['in'], POINTER(GUID), 'iid'),
                          (['in'], c_uint, 'dwClsCtx'),
                          (['in'], c_void_p, 'pActivationParams'),
                          (['out'], POINTER(c_void_p), 'ppInterface')),
                COMMETHOD([], HRESULT, 'OpenPropertyStore',
                          (['in'], c_uint, 'stgmAccess'),
                          (['out'], POINTER(c_void_p), 'ppProperties')),
                COMMETHOD([], HRESULT, 'GetId',
                          (['out'], POINTER(c_wchar_p), 'ppstrId')),
                COMMETHOD([], HRESULT, 'GetState',
                          (['out'], POINTER(c_uint), 'pdwState')),
            ]

        class IMMDeviceEnumerator(comtypes.IUnknown):
            _iid_ = GUID('{A95664D2-9614-4F35-A746-DE8DB63617E6}')
            _methods_ = [
                COMMETHOD([], HRESULT, 'EnumAudioEndpoints',
                          (['in'], c_uint, 'dataFlow'),
                          (['in'], c_uint, 'dwStateMask'),
                          (['out'], POINTER(c_void_p), 'ppDevices')),
                COMMETHOD([], HRESULT, 'GetDefaultAudioEndpoint',
                          (['in'], c_uint, 'dataFlow'),
                          (['in'], c_uint, 'role'),
                          (['out'], POINTER(POINTER(IMMDevice)), 'ppEndpoint')),
            ]

        _IMMDeviceEnumerator = IMMDeviceEnumerator
        _CLSID_MMDeviceEnumerator = GUID('{BCDE0395-E52F-467C-8E3D-C4579291692E}')
        _comtypes_ready = True
        return True
    except Exception:
        log("_init_mmdevice_com()", exc_info=True)
        return False


def _get_default_audio_endpoint_id() -> str:
    """Get the current default audio output endpoint ID via MMDeviceAPI COM."""
    try:
        if not _init_mmdevice_com():
            return ""
        import comtypes                         # pylint: disable=import-outside-toplevel
        enumerator = comtypes.CoCreateInstance(_CLSID_MMDeviceEnumerator, interface=_IMMDeviceEnumerator)
        device = enumerator.GetDefaultAudioEndpoint(0, 0)       # eRender, eConsole
        return device.GetId() or ""
    except Exception:
        log("_get_default_audio_endpoint_id()", exc_info=True)
        return ""


def _device_change_callback(*args) -> None:
    global _last_endpoint_id
    current = _get_default_audio_endpoint_id()
    if current and current == _last_endpoint_id:
        log("audio device_change(%s) endpoint unchanged: %s", args, current)
        return
    log("audio device_change(%s) endpoint changed: %s -> %s", args, _last_endpoint_id, current)
    _last_endpoint_id = current
    from xpra.platform.audio import _fire_audio_device_change
    _fire_audio_device_change()


def add_audio_device_change_callback(callback) -> None:
    global _last_endpoint_id
    from xpra.platform.audio import _audio_device_change_callbacks
    if len(_audio_device_change_callbacks) == 0:
        _last_endpoint_id = _get_default_audio_endpoint_id()
        el = get_win32_event_listener()
        if el:
            el.add_event_callback(WM_DEVICECHANGE, _device_change_callback)
    _audio_device_change_callbacks.append(callback)


def remove_audio_device_change_callback(callback) -> None:
    from xpra.platform.audio import _audio_device_change_callbacks
    if callback in _audio_device_change_callbacks:
        _audio_device_change_callbacks.remove(callback)
    if len(_audio_device_change_callbacks) == 0:
        el = get_win32_event_listener(False)
        if el:
            el.remove_event_callback(WM_DEVICECHANGE, _device_change_callback)


def is_recoverable_audio_error(error_str: str) -> bool:
    upper = error_str.upper()
    return "DEVICE_INVALIDATED" in upper or "88890004" in upper
