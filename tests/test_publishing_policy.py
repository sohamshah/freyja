import ast
import unittest
from pathlib import Path


class PublishingPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = Path(__file__).resolve().parents[1] / "bridge" / "freyja_bridge.py"
        tree = ast.parse(source.read_text())
        assignment = next(
            node
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "_EXECUTING_ACTIONS_BLOCK"
                for target in node.targets
            )
        )
        cls.policy = ast.literal_eval(assignment.value)

    def test_standing_authorization_covers_pr_review_publication(self):
        self.assertIn("durable standing instruction", self.policy)
        self.assertIn("Honor matching standing authorization without asking again", self.policy)
        self.assertIn("review findings and inline comments when later asked to review PRs", self.policy)
        self.assertNotIn("unless they asked you to in this turn", self.policy)

    def test_authorization_stays_scoped(self):
        self.assertIn("covers the action, destination and scope", self.policy)
        self.assertIn("but not merges, pushes or unrelated messages", self.policy)
        self.assertIn("Use only the review event types their instructions authorize", self.policy)

    def test_missing_authorization_or_unknown_identity_requires_confirmation(self):
        self.assertIn("does not by itself authorize publication", self.policy)
        self.assertIn("If no matching authorization exists, show the draft and ask", self.policy)
        self.assertIn("cannot tell which identity a tool posts as, ask first", self.policy)
