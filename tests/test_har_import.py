import json
import unittest

from mini_grok_api.har_import import parse_grok_har


class HarImportTests(unittest.TestCase):
    def test_parse_grok_cookie_and_user_agent(self) -> None:
        user_agent = (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
        )
        har = {
            "log": {
                "entries": [
                    {
                        "request": {
                            "url": "https://grok.com/rest/app-chat/conversations/new",
                            "headers": [
                                {"name": "User-Agent", "value": user_agent},
                                {"name": "Cookie", "value": "sso=abc; cf_clearance=xyz"},
                            ],
                        }
                    }
                ]
            }
        }

        result = parse_grok_har(json.dumps(har).encode())

        self.assertEqual(result.cookie, "sso=abc; cf_clearance=xyz")
        self.assertEqual(result.user_agent, user_agent)
        self.assertTrue(result.browser.startswith("chrome"))


if __name__ == "__main__":
    unittest.main()
