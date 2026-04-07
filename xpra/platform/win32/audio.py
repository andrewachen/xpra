# This file is part of Xpra.
# Copyright (C) 2026 Netflix, Inc.
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.
# ABOUTME: Win32 audio device change detection via MMDeviceAPI COM notifications.
# ABOUTME: Tracks the default WASAPI endpoint and fires callbacks on change.

from xpra.log import Logger

log = Logger("audio")

_notifier = None
_enumerator = None
_comtypes_ready: bool = False
_IMMNotificationClient = None
_IMMDeviceEnumerator = None
_CLSID_MMDeviceEnumerator = None


def _init_mmdevice_com() -> bool:
    """Initialize COM and MMDeviceAPI interface definitions (once)."""
    global _comtypes_ready, _IMMNotificationClient, _IMMDeviceEnumerator, _CLSID_MMDeviceEnumerator
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
                COMMETHOD([], HRESULT, 'GetDevice',
                          (['in'], c_wchar_p, 'pwstrId'),
                          (['out'], POINTER(POINTER(IMMDevice)), 'ppDevice')),
                COMMETHOD([], HRESULT, 'RegisterEndpointNotificationCallback',
                          (['in'], c_void_p, 'pClient')),
                COMMETHOD([], HRESULT, 'UnregisterEndpointNotificationCallback',
                          (['in'], c_void_p, 'pClient')),
            ]

        # IMMNotificationClient — implemented by us, called back by Windows:
        class IMMNotificationClient(comtypes.IUnknown):
            _iid_ = GUID('{7991EEC9-7E89-4D85-8390-6C703CEC60C0}')
            _methods_ = [
                COMMETHOD([], HRESULT, 'OnDeviceStateChanged',
                          (['in'], c_wchar_p, 'pwstrDeviceId'),
                          (['in'], c_uint, 'dwNewState')),
                COMMETHOD([], HRESULT, 'OnDeviceAdded',
                          (['in'], c_wchar_p, 'pwstrDeviceId')),
                COMMETHOD([], HRESULT, 'OnDeviceRemoved',
                          (['in'], c_wchar_p, 'pwstrDeviceId')),
                COMMETHOD([], HRESULT, 'OnDefaultDeviceChanged',
                          (['in'], c_uint, 'flow'),
                          (['in'], c_uint, 'role'),
                          (['in'], c_wchar_p, 'pwstrDefaultDeviceId')),
                COMMETHOD([], HRESULT, 'OnPropertyValueChanged',
                          (['in'], c_wchar_p, 'pwstrDeviceId'),
                          (['in'], c_void_p, 'key')),
            ]

        _IMMNotificationClient = IMMNotificationClient
        _IMMDeviceEnumerator = IMMDeviceEnumerator
        _CLSID_MMDeviceEnumerator = GUID('{BCDE0395-E52F-467C-8E3D-C4579291692E}')
        _comtypes_ready = True
        return True
    except Exception:
        log("_init_mmdevice_com()", exc_info=True)
        return False


def _fire_change() -> None:
    from xpra.platform.audio import _fire_audio_device_change
    _fire_audio_device_change()


def _create_notifier():
    """Create a COMObject that implements IMMNotificationClient."""
    import comtypes                             # pylint: disable=import-outside-toplevel

    class AudioEndpointNotifier(comtypes.COMObject):
        _com_interfaces_ = [_IMMNotificationClient]

        def OnDefaultDeviceChanged(self, flow, role, pwstrDefaultDeviceId):
            # eRender=0, eConsole=0 — only react to render+console changes:
            if flow == 0 and role == 0:
                log("default audio endpoint changed: %s", pwstrDefaultDeviceId)
                _fire_change()
            return 0    # S_OK

        def OnDeviceStateChanged(self, pwstrDeviceId, dwNewState):
            log("audio device state changed: %s -> %s", pwstrDeviceId, dwNewState)
            _fire_change()
            return 0

        def OnDeviceAdded(self, pwstrDeviceId):
            log("audio device added: %s", pwstrDeviceId)
            _fire_change()
            return 0

        def OnDeviceRemoved(self, pwstrDeviceId):
            log("audio device removed: %s", pwstrDeviceId)
            _fire_change()
            return 0

        def OnPropertyValueChanged(self, pwstrDeviceId, key):
            log("audio device property changed: %s", pwstrDeviceId)
            return 0    # don't fire for property changes

    return AudioEndpointNotifier()


def add_audio_device_change_callback(callback) -> None:
    global _notifier, _enumerator
    from xpra.platform.audio import _audio_device_change_callbacks
    _audio_device_change_callbacks.append(callback)
    if _notifier is not None:
        return
    if not _init_mmdevice_com():
        log.warn("Warning: cannot monitor audio device changes (comtypes not available)")
        return
    try:
        import comtypes                         # pylint: disable=import-outside-toplevel
        _enumerator = comtypes.CoCreateInstance(_CLSID_MMDeviceEnumerator, interface=_IMMDeviceEnumerator)
        _notifier = _create_notifier()
        _enumerator.RegisterEndpointNotificationCallback(_notifier)
        log("registered audio endpoint notification callback")
    except Exception:
        log("add_audio_device_change_callback()", exc_info=True)
        log.warn("Warning: failed to register audio device change notifications")
        _notifier = None
        _enumerator = None


def remove_audio_device_change_callback(callback) -> None:
    global _notifier, _enumerator
    from xpra.platform.audio import _audio_device_change_callbacks
    if callback in _audio_device_change_callbacks:
        _audio_device_change_callbacks.remove(callback)
    if len(_audio_device_change_callbacks) == 0 and _enumerator and _notifier:
        try:
            _enumerator.UnregisterEndpointNotificationCallback(_notifier)
            log("unregistered audio endpoint notification callback")
        except Exception:
            log("remove_audio_device_change_callback()", exc_info=True)
        _notifier = None
        _enumerator = None


def is_recoverable_audio_error(error_str: str) -> bool:
    upper = error_str.upper()
    return "DEVICE_INVALIDATED" in upper or "88890004" in upper
