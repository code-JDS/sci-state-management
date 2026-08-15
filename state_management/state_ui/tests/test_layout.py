from __future__ import annotations

import random
import unittest

from state_management.state_ui.layout import compute_layout


def task(number: int, dependencies: list[int] | None = None) -> dict:
    return {
        "task_id": f"T-{number:03d}",
        "dependencies": [f"T-{item:03d}" for item in dependencies or []],
    }


class LayoutTests(unittest.TestCase):
    def test_longest_dependency_chain_sets_vertical_levels(self) -> None:
        layout = compute_layout(
            [task(1), task(2), task(3, [1, 2]), task(4, [1]), task(5, [3, 4])]
        )
        positions = layout.positions
        self.assertEqual(positions["T-001"].level, 0)
        self.assertEqual(positions["T-002"].level, 0)
        self.assertEqual(positions["T-003"].level, 1)
        self.assertEqual(positions["T-004"].level, 1)
        self.assertEqual(positions["T-005"].level, 2)
        for source, target in layout.edges:
            self.assertLess(positions[source].top, positions[target].top)

    def test_predecessor_barycenter_orders_crossed_branches(self) -> None:
        layout = compute_layout(
            [task(1), task(2), task(3, [2]), task(4, [1])]
        )
        self.assertLess(layout.positions["T-004"].left, layout.positions["T-003"].left)

    def test_layout_is_deterministic_regardless_of_input_order(self) -> None:
        tasks = [task(1), task(2), task(3, [1]), task(4, [1, 2]), task(5, [3, 4])]
        expected = compute_layout(tasks)
        random.Random(19).shuffle(tasks)
        actual = compute_layout(tasks)
        self.assertEqual(actual, expected)

    def test_fifty_node_branching_graph_has_no_overlapping_nodes(self) -> None:
        tasks = [task(number) for number in range(1, 11)]
        tasks.extend(task(number, [1 + (number - 11) % 10]) for number in range(11, 26))
        tasks.extend(
            task(number, [11 + (number - 26) % 15, 11 + (number - 23) % 15])
            for number in range(26, 41)
        )
        tasks.extend(task(number, [26 + (number - 41) % 15]) for number in range(41, 51))
        layout = compute_layout(tasks)
        self.assertEqual(len(layout.positions), 50)
        self.assertEqual(len(layout.edges), sum(len(item["dependencies"]) for item in tasks))

        positions = list(layout.positions.values())
        for index, first in enumerate(positions):
            for second in positions[index + 1 :]:
                separated = (
                    first.left + first.width <= second.left
                    or second.left + second.width <= first.left
                    or first.top + first.height <= second.top
                    or second.top + second.height <= first.top
                )
                self.assertTrue(separated, (first, second))

    def test_cycle_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "cyclic task dependency"):
            compute_layout([task(1, [2]), task(2, [1])])


if __name__ == "__main__":
    unittest.main()
