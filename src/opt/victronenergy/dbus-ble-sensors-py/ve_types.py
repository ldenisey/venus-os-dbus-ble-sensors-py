from enum import IntEnum


class VeDataBasicType(IntEnum):
    VE_HEAP_STR = 0
    VE_UN8 = 1
    VE_SN8 = 2
    VE_UN16 = 3
    VE_SN16 = 4
    VE_UN24 = 5
    VE_SN24 = 6
    VE_UN32 = 7
    VE_SN32 = 8
    VE_FLOAT = 9

    def is_int(self) -> bool:
        """
        Is the type an int ?
        """
        return int(self) >= VeDataBasicType.VE_UN8 and int(self) <= VeDataBasicType.VE_SN32

    def int_size(self) -> int:
        """
        Returns int type number of bytes
        """
        return (int(self) + 1) // 2

    def is_int_signed(self) -> bool:
        """
        Is int type signed or unsigned ?
        """
        return not (int(self) & 1)


def is_int(_type: VeDataBasicType) -> bool:
    """
    Is the given type an int ?
    """
    if int(_type) < VeDataBasicType.VE_UN8:
        return False
    return int(_type) <= VeDataBasicType.VE_SN32


def int_size(_type: VeDataBasicType) -> int:
    """
    Returns type's number of bytes
    """
    return (int(_type) + 1) // 2


def is_int_signed(_type: VeDataBasicType) -> bool:
    """
    Is int type signed or unsigned ?
    """
    return not (int(_type) & 1)


def int_zext(_int: int, bits: int) -> int:
    """
    Zero-extend an unsigned int encoded in 'bits' bits to Python int
    """
    mask = (1 << bits) - 1
    return _int & mask


def int_sext(_int: int, bits: int) -> int:
    """
    Sign-extend a signed int encoded in 'bits' bits to Python int
    """
    _int = int_zext(_int, bits)
    sign_bit = 1 << (bits - 1)
    if _int & sign_bit:
        return _int - (1 << bits)
    return _int


# Explicitly expose enum members in the module namespace
VE_HEAP_STR = VeDataBasicType.VE_HEAP_STR
VE_UN8 = VeDataBasicType.VE_UN8
VE_SN8 = VeDataBasicType.VE_SN8
VE_UN16 = VeDataBasicType.VE_UN16
VE_SN16 = VeDataBasicType.VE_SN16
VE_UN24 = VeDataBasicType.VE_UN24
VE_SN24 = VeDataBasicType.VE_SN24
VE_UN32 = VeDataBasicType.VE_UN32
VE_SN32 = VeDataBasicType.VE_SN32
VE_FLOAT = VeDataBasicType.VE_FLOAT

# Define __all__ to control what gets imported with `from module import *`
__all__ = [
    'VeDataBasicType',
    'VE_HEAP_STR', 'VE_UN8', 'VE_SN8', 'VE_UN16', 'VE_SN16',
    'VE_UN24', 'VE_SN24', 'VE_UN32', 'VE_SN32', 'VE_FLOAT',
    'is_int', 'int_size', 'is_int_signed', 'int_zext', 'int_sext'
]
