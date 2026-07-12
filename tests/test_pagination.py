import unittest

from app.main import pagination_bounds


class PaginationTests(unittest.TestCase):
    def test_boundaries_and_clamping(self):
        self.assertEqual(pagination_bounds(0, 1), (1, 1, 0, 0))
        self.assertEqual(pagination_bounds(1, 1), (1, 1, 0, 1))
        self.assertEqual(pagination_bounds(10, 1), (1, 1, 0, 10))
        self.assertEqual(pagination_bounds(11, 2), (2, 2, 10, 11))
        self.assertEqual(pagination_bounds(89, 99), (9, 9, 80, 89))


if __name__ == "__main__":
    unittest.main()
