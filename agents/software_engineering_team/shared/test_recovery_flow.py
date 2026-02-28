#!/usr/bin/env python3
"""Test script to verify the 3-step truncation recovery flow.

This script tests:
1. Continuation module functionality
2. Post-mortem writing
3. Integration with LLM client

Run from the shared directory:
    python test_recovery_flow.py
"""

import sys
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import unittest
from unittest.mock import Mock, MagicMock, patch
import tempfile
import shutil


class TestContinuationModule(unittest.TestCase):
    """Test the ResponseContinuator class."""

    def test_continuation_result_dataclass(self):
        """Test ContinuationResult initialization."""
        from shared.continuation import ContinuationResult

        result = ContinuationResult(
            success=True,
            content="test content",
            cycles_used=2,
            partial_responses=["part1", "part2"],
            final_done_reason="stop",
        )

        self.assertTrue(result.success)
        self.assertEqual(result.content, "test content")
        self.assertEqual(result.cycles_used, 2)
        self.assertEqual(len(result.partial_responses), 2)
        self.assertEqual(result.final_done_reason, "stop")

    def test_continuation_prompt_creation(self):
        """Test that continuation prompts are created correctly."""
        from shared.continuation import ResponseContinuator

        continuator = ResponseContinuator(
            base_url="http://localhost:11434",
            model="test-model",
        )

        partial = "This is a partial response that was truncated"
        prompt = continuator._create_continuation_prompt(partial)

        self.assertIn("continue exactly from where you left off", prompt.lower())
        self.assertIn(partial[-100:], prompt)

    def test_message_building(self):
        """Test that conversation messages are built correctly."""
        from shared.continuation import ResponseContinuator

        continuator = ResponseContinuator(
            base_url="http://localhost:11434",
            model="test-model",
        )

        messages = continuator._build_continuation_messages(
            original_prompt="Generate a JSON object",
            partial_responses=["partial response 1"],
            system_prompt="You are a JSON generator",
        )

        self.assertEqual(len(messages), 4)
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[1]["role"], "user")
        self.assertEqual(messages[2]["role"], "assistant")
        self.assertEqual(messages[3]["role"], "user")

    def test_overlap_detection(self):
        """Test overlap detection between responses."""
        from shared.continuation import ResponseContinuator

        continuator = ResponseContinuator(
            base_url="http://localhost:11434",
            model="test-model",
        )

        text1 = "This is some text that ends with overlap here"
        text2 = "overlap here and continues with more text"

        overlap = continuator._find_overlap(text1, text2)
        self.assertEqual(overlap, len("overlap here"))

    def test_response_merging(self):
        """Test merging of partial responses."""
        from shared.continuation import ResponseContinuator

        continuator = ResponseContinuator(
            base_url="http://localhost:11434",
            model="test-model",
        )

        partial_responses = [
            '{"key1": "value1", "data": [',
            '"item1", "item2"',
            ', "item3"]}',
        ]

        merged = continuator._merge_responses(partial_responses)
        self.assertEqual(merged, '{"key1": "value1", "data": ["item1", "item2", "item3"]}')


class TestPostMortemModule(unittest.TestCase):
    """Test the PostMortemWriter class."""

    def setUp(self):
        """Create a temporary directory for test files."""
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        """Clean up temporary directory."""
        shutil.rmtree(self.temp_dir)

    def test_post_mortem_creation(self):
        """Test that post-mortems are created correctly."""
        from shared.post_mortem import PostMortemWriter

        writer = PostMortemWriter(project_root=Path(self.temp_dir))

        path = writer.write_failure(
            agent_name="TestAgent",
            task_description="Test task that failed",
            original_prompt="Generate some JSON",
            partial_responses=["partial1", "partial2"],
            continuation_attempts=5,
            decomposition_depth=3,
            error=RuntimeError("Test error"),
        )

        self.assertTrue(path.exists())
        content = path.read_text()

        self.assertIn("TestAgent", content)
        self.assertIn("Test task that failed", content)
        self.assertIn("5/5", content)
        self.assertIn("3/20", content)
        self.assertIn("Test error", content)

    def test_post_mortem_appending(self):
        """Test that multiple post-mortems are appended."""
        from shared.post_mortem import PostMortemWriter

        writer = PostMortemWriter(project_root=Path(self.temp_dir))

        writer.write_failure(
            agent_name="Agent1",
            task_description="First failure",
            original_prompt="prompt1",
            partial_responses=[],
            continuation_attempts=5,
            decomposition_depth=1,
            error=RuntimeError("Error 1"),
        )

        writer.write_failure(
            agent_name="Agent2",
            task_description="Second failure",
            original_prompt="prompt2",
            partial_responses=[],
            continuation_attempts=3,
            decomposition_depth=2,
            error=RuntimeError("Error 2"),
        )

        content = writer.post_mortem_file.read_text()

        self.assertIn("Agent1", content)
        self.assertIn("Agent2", content)
        self.assertIn("First failure", content)
        self.assertIn("Second failure", content)


class TestDecompositionContext(unittest.TestCase):
    """Test the DecompositionContext class."""

    def test_context_tracking(self):
        """Test that context tracks continuation state."""
        from shared.decomposition import DecompositionContext

        context = DecompositionContext(
            original_task="Test task",
            original_content="Test content",
        )

        self.assertFalse(context.continuation_attempted)
        self.assertEqual(len(context._partial_responses), 0)

        context.mark_continuation_attempted()
        self.assertTrue(context.continuation_attempted)

        context.add_partial_response("partial1")
        self.assertEqual(len(context._partial_responses), 1)

    def test_child_context_inherits_state(self):
        """Test that child contexts inherit continuation state."""
        from shared.decomposition import DecompositionContext

        parent = DecompositionContext(
            original_task="Test task",
            original_content="Test content",
        )

        parent.mark_continuation_attempted()
        parent.add_partial_response("partial1")

        child = parent.create_child(0, 2)

        self.assertTrue(child.continuation_attempted)
        self.assertEqual(len(child._partial_responses), 1)


def run_tests():
    """Run all tests."""
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()

    suite.addTests(loader.loadTestsFromTestCase(TestContinuationModule))
    suite.addTests(loader.loadTestsFromTestCase(TestPostMortemModule))
    suite.addTests(loader.loadTestsFromTestCase(TestDecompositionContext))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(run_tests())
