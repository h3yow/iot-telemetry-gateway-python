import unittest


class SmokeTest(unittest.TestCase):
    def test_imports(self) -> None:
        import gateway  # noqa: F401
        import gateway.api  # noqa: F401
        import gateway.service  # noqa: F401


if __name__ == "__main__":
    unittest.main()
