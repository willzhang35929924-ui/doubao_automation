"""Caption configuration never touches the user's real config directory."""

import builtins
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from doubao_automation import caption_settings
from doubao_automation import caption_runtime


class CaptionSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="caption-settings-test-")
        self.addCleanup(self.temp.cleanup)
        self.config = Path(self.temp.name) / "config"
        scoped = patch.object(caption_settings, "CONFIG_DIR", self.config)
        scoped.start()
        self.addCleanup(scoped.stop)
        self.defaults = {
            "image_prompt": "默认图片识别", "product_video_prompt": "默认商品视频识别",
            "reference_video_prompt": "默认创意视频识别",
        }
        default_patch = patch.object(caption_runtime, "DEFAULT_CAPTION_PROMPTS", self.defaults, create=True)
        default_patch.start()
        self.addCleanup(default_patch.stop)

    def test_defaults_and_cloud_key_are_private_and_encrypted(self):
        self.assertEqual(caption_settings.read_caption_settings(),
                         dict(base_url="", model="", api_key="", **self.defaults))
        self.assertEqual(caption_settings.public_caption_settings()["mode"], "local")
        public = caption_settings.save_caption_settings({
            "base_url": " HTTPS://EXAMPLE.COM/v1/ ", "model": "vision-model",
            "api_key": "my-private-test-key", "image_prompt": "定制商品识别",
        })
        self.assertEqual(public["base_url"], "https://example.com/v1")
        self.assertEqual(public["mode"], "cloud")
        self.assertEqual(public["api_key_mask"], "****")
        self.assertTrue(public["api_key_configured"])
        self.assertNotIn("api_key", public)
        self.assertEqual(caption_settings.read_caption_settings()["api_key"], "my-private-test-key")
        self.assertEqual(caption_settings.read_caption_settings()["image_prompt"], "定制商品识别")
        settings_file = self.config / "caption_settings.json"
        key_file = self.config / "caption_settings.key"
        self.assertNotIn("my-private-test-key", settings_file.read_text())
        self.assertNotIn("my-private-test-key", key_file.read_text())
        self.assertTrue(json.loads(settings_file.read_text())["api_key_ciphertext"])
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(key_file.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(settings_file.stat().st_mode), 0o600)

    def test_same_address_blank_key_preserves_and_new_address_clears(self):
        caption_settings.save_caption_settings({
            "base_url": "https://EXAMPLE.com/v1/", "model": "m", "api_key": "old-private-key",
        })
        kept = caption_settings.save_caption_settings({
            "base_url": "https://example.com/v1", "model": "m2", "api_key": "",
        })
        self.assertEqual(kept["mode"], "cloud")
        self.assertEqual(caption_settings.read_caption_settings()["api_key"], "old-private-key")
        changed = caption_settings.save_caption_settings({
            "base_url": "https://other.example/v1", "model": "m2", "api_key": "",
        })
        self.assertEqual(changed["mode"], "local")
        self.assertFalse(changed["api_key_configured"])
        self.assertEqual(caption_settings.read_caption_settings()["api_key"], "")
        self.assertEqual(json.loads((self.config / "caption_settings.json").read_text())["api_key_ciphertext"], "")

    def test_clear_key_blank_model_and_prompt_reset(self):
        caption_settings.save_caption_settings({
            "base_url": "https://example.com", "model": "m", "api_key": "secret",
            "image_prompt": "custom", "product_video_prompt": "product custom",
        })
        result = caption_settings.save_caption_settings({
            "clear_api_key": True, "api_key": "ignored", "model": "",
            "image_prompt": "  ", "product_video_prompt": "",
        })
        self.assertEqual(result["mode"], "local")
        self.assertFalse(result["api_key_configured"])
        saved = caption_settings.read_caption_settings()
        self.assertEqual(saved["api_key"], "")
        self.assertEqual(saved["model"], "")
        self.assertEqual(saved["image_prompt"], self.defaults["image_prompt"])
        self.assertEqual(saved["product_video_prompt"], self.defaults["product_video_prompt"])
        self.assertEqual(saved["reference_video_prompt"], self.defaults["reference_video_prompt"])

    def test_url_rejects_insecure_or_credential_bearing_values(self):
        for invalid in (
            "http://example.com/v1", "https://user:password@example.com/v1",
            "https://example.com/v1?token=secret", "https://example.com/v1#secret",
            "https://example.com:bad/v1", "https://example.com\\@other.invalid/v1",
            "https://example.com/v1\nX-Header: secret", "https://",
        ):
            with self.subTest(url=invalid.split("?")[0].split("#")[0]):
                with self.assertRaises(caption_settings.CaptionSettingsError) as error:
                    caption_settings.save_caption_settings({"base_url": invalid})
                self.assertNotIn("secret", str(error.exception))
                self.assertNotIn("password", str(error.exception))
        self.assertFalse(self.config.exists())

    def test_invalid_unicode_key_is_rejected_without_writing_secret(self):
        with self.assertRaises(caption_settings.CaptionSettingsError) as error:
            caption_settings.save_caption_settings({
                "base_url": "https://example.com", "model": "m", "api_key": "bad\ud800key",
            })
        self.assertNotIn("bad", str(error.exception))
        self.assertFalse(self.config.exists())

    def test_invalid_limits_preserve_previous_encrypted_settings_atomically(self):
        caption_settings.save_caption_settings({
            "base_url": "https://example.com/v1", "model": "m", "api_key": "private-key",
        })
        settings_file = self.config / "caption_settings.json"
        key_file = self.config / "caption_settings.key"
        before_file = settings_file.read_bytes()
        before_key = key_file.read_bytes()
        before_private = caption_settings.read_caption_settings()
        for invalid in (
            {"base_url": "https://example.com/" + "x" * 2049},
            {"base_url": "https://example.com/\u00a0v1"},
            {"base_url": "https://example.com/\x7fv1"},
            {"model": "m" * 257},
            {"image_prompt": "p" * (caption_runtime.MAX_CAPTION_PROMPT_LENGTH + 1)},
            {"product_video_prompt": "p" * (caption_runtime.MAX_CAPTION_PROMPT_LENGTH + 1)},
            {"reference_video_prompt": "p" * (caption_runtime.MAX_CAPTION_PROMPT_LENGTH + 1)},
            {"api_key": "bad key"},
            {"api_key": "中文密钥"},
        ):
            with self.subTest(field=next(iter(invalid))):
                with self.assertRaises(caption_settings.CaptionSettingsError):
                    caption_settings.save_caption_settings(invalid)
                self.assertEqual(settings_file.read_bytes(), before_file)
                self.assertEqual(key_file.read_bytes(), before_key)
                self.assertEqual(caption_settings.read_caption_settings(), before_private)

    def test_local_settings_work_without_crypto_and_cloud_gives_install_message(self):
        real_import = builtins.__import__

        def without_crypto(name, *args, **kwargs):
            if name.startswith("cryptography"):
                raise ModuleNotFoundError("missing cryptography", name="cryptography")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=without_crypto):
            self.assertEqual(caption_settings.public_caption_settings()["mode"], "local")
            saved = caption_settings.save_caption_settings({"image_prompt": "本地自定义识别"})
            self.assertEqual(saved["mode"], "local")
            self.assertEqual(caption_settings.read_caption_settings()["image_prompt"], "本地自定义识别")
            before = (self.config / "caption_settings.json").read_bytes()
            with self.assertRaises(caption_settings.CaptionSettingsError) as error:
                caption_settings.save_caption_settings({
                    "base_url": "https://example.com", "model": "m", "api_key": "test-key",
                })
            self.assertIn("requirements.txt", str(error.exception))
            self.assertEqual((self.config / "caption_settings.json").read_bytes(), before)
            self.assertFalse((self.config / "caption_settings.key").exists())
        caption_settings.save_caption_settings({
            "base_url": "https://example.com", "model": "m", "api_key": "test-key",
        })
        with patch("builtins.__import__", side_effect=without_crypto):
            with self.assertRaises(caption_settings.CaptionSettingsError) as error:
                caption_settings.public_caption_settings()
            self.assertIn("requirements.txt", str(error.exception))

    def test_corrupt_configuration_and_lost_key_fail_closed(self):
        self.config.mkdir()
        settings_file = self.config / "caption_settings.json"
        settings_file.write_text("{bad-json", encoding="utf-8")
        with self.assertRaises(caption_settings.CaptionSettingsError):
            caption_settings.read_caption_settings()
        with self.assertRaises(caption_settings.CaptionSettingsError):
            caption_settings.save_caption_settings({"model": "m"})
        settings_file.unlink()
        caption_settings.save_caption_settings({
            "base_url": "https://example.com", "model": "m", "api_key": "secret",
        })
        (self.config / "caption_settings.key").unlink()
        with self.assertRaises(caption_settings.CaptionSettingsError):
            caption_settings.public_caption_settings()
        with self.assertRaises(caption_settings.CaptionSettingsError):
            caption_settings.save_caption_settings({"clear_api_key": True})

    def test_tampered_saved_url_cannot_reuse_encrypted_key(self):
        caption_settings.save_caption_settings({
            "base_url": "https://example.com", "model": "m", "api_key": "secret",
        })
        path = self.config / "caption_settings.json"
        stored = json.loads(path.read_text())
        stored["base_url"] = "https://untrusted.example"
        path.write_text(json.dumps(stored), encoding="utf-8")
        with self.assertRaises(caption_settings.CaptionSettingsError):
            caption_settings.read_caption_settings()

    @unittest.skipIf(os.name == "nt", "POSIX file mode check")
    def test_permissive_key_file_fails_closed(self):
        caption_settings.save_caption_settings({
            "base_url": "https://example.com", "model": "m", "api_key": "secret",
        })
        key_file = self.config / "caption_settings.key"
        key_file.chmod(0o644)
        with self.assertRaises(caption_settings.CaptionSettingsError):
            caption_settings.read_caption_settings()


if __name__ == "__main__":
    unittest.main()
