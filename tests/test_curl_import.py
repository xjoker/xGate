import unittest

from mini_grok_api.curl_import import parse_grok_curl


class CurlImportTests(unittest.TestCase):
    def test_parse_copy_as_curl_with_cookie_header(self) -> None:
        cmd = """curl --location 'https://grok.com/rest/skills' \\
--header 'User-Agent: Mozilla/5.0 Chrome/147.0.0.0 Safari/537.36 Edg/147.0.0.0' \\
--header 'Cookie: sso=fake; cf_clearance=fake-clearance' \\
--header 'Content-Type: application/json' \\
--data '{"locale":"en"}'"""

        result = parse_grok_curl(cmd)

        self.assertEqual(result.cookie, "sso=fake; cf_clearance=fake-clearance")
        self.assertIn("Chrome/147", result.user_agent)
        self.assertEqual(result.url, "https://grok.com/rest/skills")
        self.assertEqual(result.body, '{"locale":"en"}')

    def test_parse_copy_as_curl_with_cookie_flag(self) -> None:
        cmd = """curl 'https://grok.com/rest/skills' \\
-H 'user-agent: Mozilla/5.0 Chrome/147.0.0.0 Safari/537.36' \\
-b 'sso=fake; cf_clearance=fake-clearance' \\
--data-raw '{"locale":"en"}'"""

        result = parse_grok_curl(cmd)

        self.assertEqual(result.cookie, "sso=fake; cf_clearance=fake-clearance")
        self.assertTrue(result.browser.startswith("chrome"))


if __name__ == "__main__":
    unittest.main()
