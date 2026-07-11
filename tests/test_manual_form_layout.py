import unittest

from app.main import MANUAL_FORM_FIELDS, manual_form_position


class ManualFormLayoutTests(unittest.TestCase):
    def test_eight_fields_use_two_groups_per_row(self):
        self.assertEqual(
            [manual_form_position(index) for index in range(len(MANUAL_FORM_FIELDS))],
            [(0, 0), (0, 1), (1, 0), (1, 1), (2, 0), (2, 1), (3, 0), (3, 1)],
        )

    def test_grid_positions_are_unique(self):
        positions = {manual_form_position(index) for index in range(len(MANUAL_FORM_FIELDS))}
        self.assertEqual(len(positions), len(MANUAL_FORM_FIELDS))

    def test_invalid_field_position_is_rejected(self):
        with self.assertRaises(IndexError):
            manual_form_position(len(MANUAL_FORM_FIELDS))


if __name__ == "__main__":
    unittest.main()
