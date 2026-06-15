import pytest
import usb.core
from unittest.mock import MagicMock, patch


class USBExpectation:
    def __init__(self, op_type, **kwargs):
        self.op_type = op_type
        self.params = kwargs
        self.response = b''
        self.error = None

    def respond_with(self, data):
        self.response = data
        return self

    def error_with(self, error):
        self.error = error
        return self

    def __repr__(self):
        return f"<USBExpectation {self.op_type} {self.params}>"


class PyUSBMock:
    """
    A trace-replayer mock for PyUSB.
    Ensures that the driver sends exact byte sequences in the correct order.
    """
    def __init__(self):
        self.expectations = []
        self.call_index = 0
        self.device = MagicMock(spec=usb.core.Device)
        self.device._ctx = MagicMock()

        # Wire up the mock device to our replayer
        self.device.ctrl_transfer.side_effect = self._handle_ctrl
        self.device.write.side_effect = self._handle_write
        self.device.read.side_effect = self._handle_read
        self.device.set_configuration.side_effect = self._handle_nop
        self.device.clear_halt.side_effect = self._handle_nop

    def expect_ctrl(self, bmRequestType, bRequest, wValue, wIndex, data=None):
        exp = USBExpectation('ctrl', bmRequestType=bmRequestType, bRequest=bRequest, wValue=wValue, wIndex=wIndex, data=data)
        self.expectations.append(exp)
        return exp

    def expect_write(self, endpoint, data):
        exp = USBExpectation('write', endpoint=endpoint, data=data)
        self.expectations.append(exp)
        return exp

    def expect_read(self, endpoint, length=None):
        exp = USBExpectation('read', endpoint=endpoint, length=length)
        self.expectations.append(exp)
        return exp

    def _handle_ctrl(self, bmRequestType, bRequest, wValue, wIndex, data=None, timeout=None):
        exp = self._get_next_expectation('ctrl')

        # Verify parameters (handling potential None vs b'')
        if exp.params['bmRequestType'] != bmRequestType:
            pytest.fail(f"Mock mismatch (ctrl): Expected bmRequestType {hex(exp.params['bmRequestType'])}, got {hex(bmRequestType)}")
        if exp.params['bRequest'] != bRequest:
            pytest.fail(f"Mock mismatch (ctrl): Expected bRequest {hex(exp.params['bRequest'])}, got {hex(bRequest)}")
        if exp.params['wValue'] != wValue:
            pytest.fail(f"Mock mismatch (ctrl): Expected wValue {hex(exp.params['wValue'])}, got {hex(wValue)}")
        if exp.params['wIndex'] != wIndex:
            pytest.fail(f"Mock mismatch (ctrl): Expected wIndex {hex(exp.params['wIndex'])}, got {hex(wIndex)}")

        # Verify the OUT data payload, but only when the expectation pinned one: a control-IN
        # call passes an int wLength (or None) as the 5th arg, not bytes — there's no payload
        # to byte-check, and its bytes come back via exp.response.
        if exp.params['data'] is not None:
            actual_data = bytes(data) if data is not None else b''
            expected_data = bytes(exp.params['data'])
            if actual_data != expected_data:
                pytest.fail(f"Mock mismatch (ctrl) data:\nExpected: {expected_data.hex(' ')}\nGot:      {actual_data.hex(' ')}")

        if exp.error:
            raise exp.error
        return exp.response

    def _handle_write(self, endpoint, data, timeout=None):
        exp = self._get_next_expectation('write')

        if exp.params['endpoint'] != endpoint:
            pytest.fail(f"Mock mismatch (write): Expected EP {hex(exp.params['endpoint'])}, got {hex(endpoint)}")

        # Check data (support both bytes and bytearray)
        actual_data = bytes(data) if data is not None else b''
        expected_data = bytes(exp.params['data']) if exp.params['data'] is not None else b''

        if actual_data != expected_data:
            pytest.fail(f"Mock mismatch (write) on EP {hex(endpoint)}:\nExpected: {expected_data.hex(' ')}\nGot:      {actual_data.hex(' ')}")

        if exp.error:
            raise exp.error
        return len(actual_data)

    def _handle_read(self, endpoint, length, timeout=None):
        exp = self._get_next_expectation('read')

        if exp.params['endpoint'] != endpoint:
            pytest.fail(f"Mock mismatch (read): Expected EP {hex(exp.params['endpoint'])}, got {hex(endpoint)}")

        if exp.error:
            raise exp.error
        return exp.response

    def _handle_nop(self, *args, **kwargs):
        return None

    def _get_next_expectation(self, op_type):
        if self.call_index >= len(self.expectations):
            pytest.fail(f"Unexpected USB {op_type} call (No more expectations)")

        exp = self.expectations[self.call_index]
        self.call_index += 1

        if exp.op_type != op_type:
            pytest.fail(f"Mock sequence mismatch: Expected {exp.op_type}, got {op_type}")

        return exp

    def verify(self):
        """Ensure all expected calls were made."""
        if self.call_index < len(self.expectations):
            remaining = self.expectations[self.call_index:]
            pytest.fail(f"Mock verification failed: {len(remaining)} expected calls were not made: {remaining}")


@pytest.fixture
def usb_mock():
    mock = PyUSBMock()
    with patch('usb.core.find', return_value=mock.device):
        yield mock
    mock.verify()
