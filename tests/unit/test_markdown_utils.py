from augury.extract.markdown_utils import images_to_placeholders, tidy, word_count


def test_images_become_openable_placeholders():
    md = 'Intro ![Fig. 2 — architecture](https://x/f2.png "title") and ![](https://x/a.png)'
    assert images_to_placeholders(md) == (
        "Intro [image: Fig. 2 — architecture](https://x/f2.png) and [image: figure](https://x/a.png)"
    )


def test_linked_image_collapses_to_one_placeholder():
    # markdownify emits [![alt](src)](href) for a "click to enlarge" image link; CommonMark
    # doesn't allow a link inside a link, so keeping both corrupts the text.
    assert images_to_placeholders("[![a](i.png)](big.png)") == "[image: a](i.png)"
    assert images_to_placeholders("![a](i.png)") == "[image: a](i.png)"  # bare image: unchanged


def test_tidy_collapses_blank_runs_and_trailing_space():
    assert tidy("a  \n\n\n\nb\n") == "a\n\nb"


def test_word_count_ignores_link_targets():
    assert word_count("Read [the paper](https://arxiv.org/abs/1234.5678) now") == 4
