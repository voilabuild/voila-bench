import pytest

from slugify import slugify


@pytest.mark.parametrize(
    ("text", "slug"),
    [("Hello World", "hello-world"), ("  Café crème ", "cafe-creme"), ("a -- b", "a-b"), ("", "")],
)
def test_slugify(text: str, slug: str) -> None:
    assert slugify(text) == slug
