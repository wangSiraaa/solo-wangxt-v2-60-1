"""许可版本与开工资格规则：

- 所有许可变更（签发/部分撤销/恢复）版本严格递增；
- 过期确认（旧版本或已过有效期）不得恢复已失效的开工资格。
"""

import unittest

from maintenance_window import (
    ManualClock,
    PermitRegistry,
    ProtectionNotCovered,
    StalePermitVersion,
)

from .helpers import at


class PermitVersioningTest(unittest.TestCase):
    def setUp(self):
        self.clock = ManualClock(at(8))
        self.registry = PermitRegistry(self.clock)

    def test_versions_increase_across_issue_and_revoke(self):
        p1 = self.registry.issue({"P1", "P2"}, valid_until=at(13))
        p2 = self.registry.revoke({"P2"})
        p3 = self.registry.issue({"P1", "P2"}, valid_until=at(13))
        self.assertEqual([p.version for p in self.registry.permits], [1, 2, 3])
        self.assertEqual((p1.version, p2.version, p3.version), (1, 2, 3))

    def test_partial_revoke_shrinks_scope_and_invalidates_old_confirmations(self):
        self.registry.issue({"P1", "P2"}, valid_until=at(13))
        self.registry.confirm(permit_version=1, protections={"P1", "P2"},
                              by="防护员-甲", valid_until=at(12, 30))
        self.assertTrue(self.registry.is_work_eligible({"P2"}))

        self.registry.revoke({"P2"})
        # 旧版本确认不得恢复已失效的开工资格
        self.assertFalse(self.registry.is_work_eligible({"P2"}))
        self.assertFalse(self.registry.is_work_eligible({"P1"}))  # 新版本尚未确认

    def test_stale_version_confirmation_is_rejected(self):
        self.registry.issue({"P1", "P2"}, valid_until=at(13))
        self.registry.confirm(permit_version=1, protections={"P1", "P2"},
                              by="防护员-甲", valid_until=at(12, 30))
        self.registry.revoke({"P2"})
        with self.assertRaises(StalePermitVersion):
            self.registry.confirm(permit_version=1, protections={"P2"},
                                  by="防护员-甲", valid_until=at(12, 30))
        # 拒绝后资格依然失效
        self.assertFalse(self.registry.is_work_eligible({"P2"}))

    def test_expired_confirmation_does_not_grant_eligibility(self):
        self.registry.issue({"P1"}, valid_until=at(13))
        self.registry.confirm(permit_version=1, protections={"P1"},
                              by="防护员-甲", valid_until=at(9))
        self.clock.set(at(9, 30))  # 确认已过有效期
        self.assertFalse(self.registry.is_work_eligible({"P1"}))

    def test_confirmation_cannot_exceed_permit_validity(self):
        self.registry.issue({"P1"}, valid_until=at(10))
        confirmation = self.registry.confirm(permit_version=1, protections={"P1"},
                                             by="防护员-甲", valid_until=at(13))
        self.assertEqual(confirmation.valid_until, at(10))
        self.clock.set(at(10, 30))  # 许可本身已过期
        self.assertFalse(self.registry.is_work_eligible({"P1"}))

    def test_confirmation_outside_permit_scope_is_rejected(self):
        self.registry.issue({"P1"}, valid_until=at(13))
        with self.assertRaises(ProtectionNotCovered):
            self.registry.confirm(permit_version=1, protections={"P1", "P9"},
                                  by="防护员-甲", valid_until=at(12))

    def test_reconfirm_current_version_restores_only_current_scope(self):
        self.registry.issue({"P1", "P2"}, valid_until=at(13))
        self.registry.confirm(permit_version=1, protections={"P1", "P2"},
                              by="防护员-甲", valid_until=at(12, 30))
        self.registry.revoke({"P2"})
        self.registry.confirm(permit_version=2, protections={"P1"},
                              by="防护员-乙", valid_until=at(12, 30))
        self.assertTrue(self.registry.is_work_eligible({"P1"}))
        self.assertFalse(self.registry.is_work_eligible({"P2"}))  # 已撤销的保护不在当前版本内


if __name__ == "__main__":
    unittest.main()
