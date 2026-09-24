"""交接班规则：新班确认接收前旧班不得交出责任；迟到确认不生效。"""

import unittest

from maintenance_window import (
    HandoverNotConfirmed,
    HandoverState,
    LateHandoverConfirmation,
    ShiftHandover,
)

from .helpers import at


class ShiftHandoverTest(unittest.TestCase):
    def test_release_requires_receipt_confirmation_first(self):
        handover = ShiftHandover("白班", "夜班", confirm_by=at(10, 30), offered_at=at(10))
        with self.assertRaises(HandoverNotConfirmed):
            handover.release(by="白班班长", at=at(10, 10))

        handover.confirm_receipt(by="夜班班长", at=at(10, 20))
        handover.release(by="白班班长", at=at(10, 25))
        self.assertIs(handover.state, HandoverState.CLOSED)

    def test_late_confirmation_is_recorded_but_not_effective(self):
        handover = ShiftHandover("白班", "夜班", confirm_by=at(10, 30), offered_at=at(10))
        with self.assertRaises(LateHandoverConfirmation):
            handover.confirm_receipt(by="夜班班长", at=at(10, 45))

        self.assertIs(handover.state, HandoverState.OFFERED)  # 交接保持未闭合
        self.assertIsNotNone(handover.late_confirmation)
        self.assertEqual(handover.late_confirmation.by, "夜班班长")
        # 迟到确认之后旧班依然不得交出责任
        with self.assertRaises(HandoverNotConfirmed):
            handover.release(by="白班班长", at=at(10, 50))

    def test_confirm_at_deadline_is_accepted(self):
        handover = ShiftHandover("白班", "夜班", confirm_by=at(10, 30), offered_at=at(10))
        handover.confirm_receipt(by="夜班班长", at=at(10, 30))
        self.assertIs(handover.state, HandoverState.RECEIPT_CONFIRMED)


if __name__ == "__main__":
    unittest.main()
