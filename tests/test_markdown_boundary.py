from html.parser import HTMLParser
import unittest

from workflowy_importer.markdown import render_inline


class ParsedHTML(HTMLParser):
    def __init__(self, value):
        super().__init__(convert_charrefs=True)
        self.tags = []
        self.text = []
        self.feed(value)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    def handle_data(self, data):
        self.text.append(data)


class MarkdownBoundaryTests(unittest.TestCase):
    def parse_safe(self, source, resolver=None):
        parsed = ParsedHTML(render_inline(source, resolver))
        self.assertFalse(any(name.startswith('on') for _, attrs in parsed.tags for name in attrs))
        self.assertFalse(any(tag in {'img', 'script', 'iframe'} for tag, _ in parsed.tags))
        return parsed

    def test_image_source_cannot_replace_an_attribute_placeholder(self):
        source = '![x](" onmouseover="alert(1)) [click](https://example.invalid/WFYIMGTOKEN000000)'
        parsed = self.parse_safe(source)
        self.assertEqual(parsed.tags, [('a', {'href': 'https://example.invalid/WFYIMGTOKEN000000'})])
        self.assertIn('![x](" onmouseover="alert(1))', ''.join(parsed.text))

    def test_old_placeholder_strings_are_ordinary_text_in_all_contexts(self):
        for token in ('WFYIMGTOKEN000000', 'WFYLINKTOKEN000000'):
            with self.subTest(token=token):
                source = f'![literal](image.png) [[Page|{token}]] [click](https://example.invalid/{token}) `{token}`'
                parsed = self.parse_safe(source, lambda target: 'https://workflowy.com/#/fixture')
                anchors = [attrs for tag, attrs in parsed.tags if tag == 'a']
                self.assertEqual(anchors, [{'href': 'https://workflowy.com/#/fixture'}, {'href': 'https://example.invalid/' + token}])
                self.assertGreaterEqual(''.join(parsed.text).count(token), 2)

    def test_images_preserve_raw_syntax_prefix_and_nested_content_as_text(self):
        for source in ('![nested [alt]](https://example.invalid/a "title")',
                       '![alt](<https://example.invalid/a>)',
                       '![a [[Page]]](image.png)', '![**bold**](image.png)'):
            with self.subTest(source=source):
                parsed = self.parse_safe('prefix ' + source + ' suffix', lambda _: 'https://workflowy.com/#/fixture')
                self.assertEqual(parsed.tags, [])
                self.assertEqual(''.join(parsed.text), 'prefix ' + source + ' suffix')

    def test_images_and_wiki_inside_link_labels_never_nest_anchors(self):
        for label in ('![alt](image.png)', '[[Page]]'):
            parsed = self.parse_safe(f'[{label}](https://example.invalid/)', lambda _: 'https://workflowy.com/#/fixture')
            self.assertEqual(parsed.tags, [('a', {'href': 'https://example.invalid/'})])
            self.assertIn(label, ''.join(parsed.text))

    def test_wiki_labels_and_resolved_local_links_keep_their_distinct_contracts(self):
        resolver = lambda target: 'https://workflowy.com/#/fixture' if target in {'Page', 'Page.md#Heading'} else None
        parsed = self.parse_safe('[[Page|**plain label**]] [**bold label**](Page.md#Heading) [[Missing|**formatted**]] [missing](Missing.md)', resolver)
        self.assertEqual([attrs['href'] for tag, attrs in parsed.tags if tag == 'a'],
                         ['https://workflowy.com/#/fixture', 'https://workflowy.com/#/fixture', 'Missing.md'])
        self.assertIn('**plain label**', ''.join(parsed.text))
        self.assertEqual([tag for tag, _ in parsed.tags].count('b'), 2)

    def test_generated_urls_cannot_add_attributes_or_unsafe_schemes(self):
        parsed = self.parse_safe('[[Page|label]]', lambda _: 'https://example.invalid/a?x="&y=<tag>')
        self.assertEqual(len(parsed.tags), 1)
        self.assertEqual(set(parsed.tags[0][1]), {'href'})
        self.assertIn('%22', parsed.tags[0][1]['href'])
        for destination in ('javascript:alert(1)', 'data:text/html,<script>bad</script>', 'vbscript:bad'):
            with self.subTest(destination=destination):
                parsed = self.parse_safe('[[Page]] [label](Page.md)', lambda _: destination)
                self.assertEqual([attrs['href'] for tag, attrs in parsed.tags if tag == 'a'], ['Page.md'])

    def test_code_escape_and_native_inline_formatting_are_preserved(self):
        source = '`[[Page]] ![alt](image.png) WFYLINKTOKEN000000`'
        parsed = self.parse_safe(source, lambda _: 'https://workflowy.com/#/fixture')
        self.assertEqual(parsed.tags, [('code', {})])
        self.assertEqual(''.join(parsed.text), source[1:-1])
        parsed = self.parse_safe('**bold** *italic* ~~strike~~\nsoft  \nhard \\[\\[Page\\]\\]')
        self.assertEqual([tag for tag, _ in parsed.tags], ['b', 'i', 's', 'br'])
        self.assertIn('[[Page]]', ''.join(parsed.text))


if __name__ == '__main__':
    unittest.main()
