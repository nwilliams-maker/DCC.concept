"""Optional email must never split the signed-in HTML into a Markdown code block."""
from html.parser import HTMLParser
from test_revamp import load_functions


class HeaderParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self.text = []

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            self.links.append(dict(attrs))

    def handle_data(self, data):
        self.text.append(data)


def test_fn_header_without_email_has_single_name_and_working_logout_link():
    header = load_functions('tactical_workspace_master_rw.py', ['_signed_in_header_html'])['_signed_in_header_html']
    for email in ('', None, '   '):
        html = header({'name': 'Field Nation Dispatch Associate', 'role': 'Field Nation Dispatch Associate', 'pod': 'Field Nation', 'scope': 'field_nation', 'email': email})
        assert '\n' not in html
        parser = HeaderParser()
        parser.feed(html)
        assert parser.text == ['Signed in as', 'Field Nation Dispatch Associate', 'Sign out']
        assert parser.links[0]['href'] == '?logout=1'
        assert parser.links[0]['target'] == '_self'


def test_regular_header_keeps_pod_role_email_and_escapes_user_fields():
    header = load_functions('tactical_workspace_master_rw.py', ['_signed_in_header_html'])['_signed_in_header_html']
    html = header({'name': '<Nick>', 'pod': 'ADMIN', 'role': 'Manager', 'email': 'nick@example.com'})
    assert '<Nick>' not in html
    parser = HeaderParser()
    parser.feed(html)
    assert parser.text == ['Signed in as', '<Nick> · ADMIN Manager', 'nick@example.com', 'Sign out']
