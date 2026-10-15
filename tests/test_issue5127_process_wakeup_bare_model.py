"""Tests for issue #5127 — process-wakeup bare custom-provider model repair.

When ``_resolve_compatible_session_model_state`` takes the #1855 fast path
(both model and model_provider set), a bare runtime model that matches the
suffix of a slash-qualified ``profile_default_model`` must repair to the full
qualified ID for named custom providers.
"""
from unittest.mock import patch

import pytest


class TestIssue5127CustomProviderBareSuffixRepair:
    def test_profile_scoped_qualified_model_survives_cold_reload(self, monkeypatch):
        """A valid named-custom session must not revert to the profile default."""
        import api.config as config
        from api.routes import _resolve_compatible_session_model_state

        profile_cfg = {
            "custom_providers": [
                {
                    "name": "local-lab",
                    "model": "local-chat-35b-q6",
                    "models": ["local-chat-35b-q6"],
                }
            ]
        }
        monkeypatch.setattr(config, "cfg", {"model": {"provider": "openai-codex"}})
        monkeypatch.setattr(
            "api.routes.get_available_models",
            lambda **_kwargs: {
                "active_provider": "openai-codex",
                "default_model": "gpt-5.6-luna",
                "groups": [
                    {
                        "provider": "OpenAI Codex",
                        "provider_id": "openai-codex",
                        "models": [{"id": "gpt-5.6-luna", "label": "Luna"}],
                    }
                ],
            },
        )

        result = _resolve_compatible_session_model_state(
            "@custom:local-lab:local-chat-35b-q6",
            "custom:local-lab",
            profile_config=profile_cfg,
            prefer_cached_catalog=True,
        )

        assert result == (
            "@custom:local-lab:local-chat-35b-q6",
            "custom:local-lab",
            False,
        )

    def test_repairs_removed_qualified_model_to_same_custom_provider_default(self):
        """A renamed local model must not remain routable only because its provider exists."""
        from api.routes import _resolve_compatible_session_model_state

        profile_cfg = {
            "custom_providers": [
                {
                    "name": "local-lab",
                    "model": "local-chat-35b-q6",
                    "models": ["local-chat-35b-q6"],
                }
            ]
        }

        with patch("api.routes.get_available_models") as mock_catalog:
            result = _resolve_compatible_session_model_state(
                "@custom:local-lab:local-chat-35b-q4",
                "custom:local-lab",
                profile_config=profile_cfg,
                explicit_model_pick=True,
                prefer_cached_catalog=True,
            )

        assert mock_catalog.call_count == 0
        assert result == (
            "@custom:local-lab:local-chat-35b-q6",
            "custom:local-lab",
            True,
        )

    @pytest.mark.parametrize(
        "entry_extra",
        [
            {"models": {"saved-a": {}}, "models_discovered": True},
            {"models": {"saved-a": {"context_length": 128000}}},
        ],
        ids=["discovered-snapshot", "dict-metadata"],
    )
    def test_unpinned_models_mapping_does_not_repair_live_pick(self, entry_extra):
        """A discovery snapshot or metadata dict cannot prove a model was removed."""
        from api.routes import _resolve_compatible_session_model_state

        profile_cfg = {
            "custom_providers": [
                {"name": "local-lab", "model": "saved-a", **entry_extra}
            ]
        }

        with patch("api.routes.get_available_models") as mock_catalog:
            result = _resolve_compatible_session_model_state(
                "@custom:local-lab:new-live-c",
                "custom:local-lab",
                profile_config=profile_cfg,
                explicit_model_pick=True,
                prefer_cached_catalog=True,
            )

        assert mock_catalog.call_count == 0
        assert result == ("@custom:local-lab:new-live-c", "custom:local-lab", False)

    def test_no_profile_config_never_repairs_from_shared_cfg(self, monkeypatch):
        """The shared cfg may belong to another profile, so it can't drive a rewrite."""
        import api.config as config
        from api.routes import _repair_removed_qualified_custom_provider_model

        monkeypatch.setattr(
            config,
            "cfg",
            {
                "custom_providers": [
                    {"name": "local-lab", "model": "chat-new", "models": ["chat-new"]}
                ]
            },
        )

        assert (
            _repair_removed_qualified_custom_provider_model(
                "@custom:local-lab:chat-old",
                "custom:local-lab",
                config_obj=None,
            )
            is None
        )

    def test_repair_replacement_stays_inside_pinned_allowlist(self):
        """A singular default outside the pinned list is not a valid replacement."""
        from api.routes import _repair_removed_qualified_custom_provider_model

        profile_cfg = {
            "custom_providers": [
                {"name": "local-lab", "model": "chat-default", "models": ["chat-a"]}
            ]
        }

        assert (
            _repair_removed_qualified_custom_provider_model(
                "@custom:local-lab:chat-old",
                "custom:local-lab",
                config_obj=profile_cfg,
            )
            == "@custom:local-lab:chat-a"
        )

    def test_repair_fails_closed_on_colliding_slugs(self):
        """Two entries normalising to one slug must not pick a winner."""
        from api.routes import _repair_removed_qualified_custom_provider_model

        profile_cfg = {
            "custom_providers": [
                {"name": "Local Lab", "model": "chat-a", "models": ["chat-a"]},
                {"name": "local-lab", "model": "chat-old", "models": ["chat-old"]},
            ]
        }

        assert (
            _repair_removed_qualified_custom_provider_model(
                "@custom:local-lab:chat-old",
                "custom:local-lab",
                config_obj=profile_cfg,
            )
            is None
        )

    def test_fast_path_rejects_longer_parsed_qualifier(self):
        """The parsed qualifier must equal the session provider to skip the catalog."""
        from api.routes import _resolve_compatible_session_model_state

        profile_cfg = {
            "custom_providers": [
                {"name": "host.example", "model": "m", "models": ["m"]}
            ]
        }

        with patch("api.routes.get_available_models", return_value={"groups": []}) as mock_catalog:
            _resolve_compatible_session_model_state(
                "@custom:host.example:8080:m",
                "custom:host.example",
                profile_config=profile_cfg,
                prefer_cached_catalog=True,
            )

        assert mock_catalog.call_count == 1

    def test_removed_custom_provider_is_not_kept_by_unrelated_entry(self, monkeypatch):
        """Another configured custom provider must not keep a removed one routable."""
        import api.config as config
        from api.routes import _resolve_compatible_session_model_state

        profile_cfg = {
            "custom_providers": [
                {"name": "other-lab", "model": "other-chat", "models": ["other-chat"]}
            ]
        }
        monkeypatch.setattr(config, "cfg", {"model": {"provider": "openai-codex"}})
        monkeypatch.setattr(
            "api.routes.get_available_models",
            lambda **_kwargs: {
                "active_provider": "openai-codex",
                "default_model": "gpt-5.6-luna",
                "groups": [
                    {
                        "provider": "OpenAI Codex",
                        "provider_id": "openai-codex",
                        "models": [{"id": "gpt-5.6-luna", "label": "Luna"}],
                    }
                ],
            },
        )

        result = _resolve_compatible_session_model_state(
            "@custom:local-lab:local-chat-35b-q6",
            "custom:local-lab",
            profile_config=profile_cfg,
            prefer_cached_catalog=True,
        )

        assert result != (
            "@custom:local-lab:local-chat-35b-q6",
            "custom:local-lab",
            False,
        )

    def test_fast_path_repairs_bare_suffix_to_profile_default(self):
        from api.routes import _resolve_compatible_session_model_state

        with patch("api.routes.get_available_models") as mock_catalog:
            result = _resolve_compatible_session_model_state(
                "grok-composer-2.5-fast",
                "custom:my-proxy",
                profile_provider="custom:my-proxy",
                profile_default_model="x-ai/grok-composer-2.5-fast",
                prefer_cached_catalog=True,
            )

        assert mock_catalog.call_count == 0
        assert result == ("x-ai/grok-composer-2.5-fast", "custom:my-proxy", True)

    def test_fast_path_skips_repair_when_profile_provider_mismatches(self):
        """Regression: custom:other-proxy must not inherit my-proxy's qualified default."""
        from api.routes import _resolve_compatible_session_model_state

        with patch("api.routes.get_available_models") as mock_catalog:
            result = _resolve_compatible_session_model_state(
                "grok-composer-2.5-fast",
                "custom:other-proxy",
                profile_provider="custom:my-proxy",
                profile_default_model="x-ai/grok-composer-2.5-fast",
                prefer_cached_catalog=True,
            )

        assert mock_catalog.call_count == 0
        assert result == ("grok-composer-2.5-fast", "custom:other-proxy", False)

    def test_fast_path_repairs_generic_custom_provider(self):
        from api.routes import _resolve_compatible_session_model_state

        with patch("api.routes.get_available_models") as mock_catalog:
            result = _resolve_compatible_session_model_state(
                "some-model",
                "custom",
                profile_default_model="vendor/some-model",
                prefer_cached_catalog=True,
            )

        assert mock_catalog.call_count == 0
        assert result == ("vendor/some-model", "custom", True)

    def test_fast_path_does_not_rewrite_unrelated_bare_model(self):
        from api.routes import _resolve_compatible_session_model_state

        with patch("api.routes.get_available_models") as mock_catalog:
            result = _resolve_compatible_session_model_state(
                "other-model",
                "custom:my-proxy",
                profile_default_model="x-ai/grok-composer-2.5-fast",
                prefer_cached_catalog=True,
            )

        assert mock_catalog.call_count == 0
        assert result == ("other-model", "custom:my-proxy", False)

    def test_fast_path_keeps_qualified_model_unchanged(self):
        from api.routes import _resolve_compatible_session_model_state

        with patch("api.routes.get_available_models") as mock_catalog:
            result = _resolve_compatible_session_model_state(
                "x-ai/grok-composer-2.5-fast",
                "custom:my-proxy",
                profile_default_model="x-ai/grok-composer-2.5-fast",
                prefer_cached_catalog=True,
            )

        assert mock_catalog.call_count == 0
        assert result == ("x-ai/grok-composer-2.5-fast", "custom:my-proxy", False)

    def test_slow_path_repairs_bare_suffix_to_profile_default_for_custom_provider(self):
        """Regression #5225: async continuations may arrive without usable model_provider."""
        from api.routes import _resolve_compatible_session_model_state

        with patch("api.routes.get_available_models") as mock_catalog:
            mock_catalog.return_value = {
                "active_provider": "openrouter",
                "default_model": "openai/gpt-5.5",
                "groups": [],
            }
            result = _resolve_compatible_session_model_state(
                "grok-composer-2.5-fast",
                None,
                profile_provider="custom:my-proxy",
                profile_default_model="x-ai/grok-composer-2.5-fast",
                prefer_cached_catalog=True,
            )

        assert mock_catalog.call_count == 1
        assert result == ("x-ai/grok-composer-2.5-fast", "custom:my-proxy", True)

    def test_fast_path_repairs_non_default_bare_model_of_custom_provider(self):
        """Regression: non-default bare model must be dynamically repaired if listed under the custom provider."""
        from api.routes import _resolve_compatible_session_model_state

        _mock_cfg = {
            "custom_providers": [
                {
                    "name": "my-proxy",
                    "base_url": "https://llm-proxy.ext.ben.io/v1",
                    "models": {
                        "x-ai/grok-composer-2.5-fast": {},
                        "umans/umans-glm-5.2": {},
                    }
                }
            ]
        }

        with patch("api.routes.get_available_models") as mock_catalog, \
             patch("api.config.cfg", _mock_cfg):
            result = _resolve_compatible_session_model_state(
                "grok-composer-2.5-fast",
                "custom:my-proxy",
                profile_provider="custom:my-proxy",
                profile_default_model="umans/umans-glm-5.2",
                profile_config=_mock_cfg,
                prefer_cached_catalog=True,
            )

        assert mock_catalog.call_count == 0
        assert result == ("x-ai/grok-composer-2.5-fast", "custom:my-proxy", True)

    def test_slow_path_repairs_non_default_bare_model_of_custom_provider(self):
        """Regression: slow-path must also dynamically repair non-default bare model if listed under custom provider."""
        from api.routes import _resolve_compatible_session_model_state

        _mock_cfg = {
            "custom_providers": [
                {
                    "name": "my-proxy",
                    "base_url": "https://llm-proxy.ext.ben.io/v1",
                    "models": {
                        "x-ai/grok-composer-2.5-fast": {},
                        "umans/umans-glm-5.2": {},
                    }
                }
            ]
        }

        with patch("api.routes.get_available_models") as mock_catalog, \
             patch("api.config.cfg", _mock_cfg):
            mock_catalog.return_value = {
                "active_provider": "openrouter",
                "default_model": "openai/gpt-5.5",
                "groups": [],
            }
            result = _resolve_compatible_session_model_state(
                "grok-composer-2.5-fast",
                None,
                profile_provider="custom:my-proxy",
                profile_default_model="umans/umans-glm-5.2",
                profile_config=_mock_cfg,
                prefer_cached_catalog=True,
            )

        assert mock_catalog.call_count == 1
        assert result == ("x-ai/grok-composer-2.5-fast", "custom:my-proxy", True)

    def test_slow_path_does_not_rewrite_unrelated_bare_model_for_custom_provider(self):
        from api.routes import _resolve_compatible_session_model_state

        with patch("api.routes.get_available_models") as mock_catalog:
            mock_catalog.return_value = {
                "active_provider": "openrouter",
                "default_model": "openai/gpt-5.5",
                "groups": [],
            }
            result = _resolve_compatible_session_model_state(
                "other-model",
                None,
                profile_provider="custom:my-proxy",
                profile_default_model="x-ai/grok-composer-2.5-fast",
                prefer_cached_catalog=True,
            )

        assert mock_catalog.call_count == 1
        assert result == ("other-model", "custom:my-proxy", False)

    def test_openai_codex_stale_openai_slash_still_uses_slow_path(self):
        """Regression: openai/... under openai-codex must not take bare fast return."""
        from api.routes import _resolve_compatible_session_model_state

        with patch("api.routes.get_available_models") as mock_catalog:
            mock_catalog.return_value = {
                "active_provider": "openai-codex",
                "default_model": "gpt-5.5",
                "groups": [],
            }
            result = _resolve_compatible_session_model_state(
                "openai/gpt-5.4-mini",
                "openai-codex",
                profile_default_model="gpt-5.5",
                prefer_cached_catalog=True,
            )

        assert mock_catalog.call_count >= 1
        assert result[0] == "gpt-5.5"
        assert result[2] is True


class TestRepairBareCustomProviderModel:
    def test_uses_config_order_when_suffixes_collide(self):
        from api.routes import _repair_bare_custom_provider_model

        custom_cfg = [
            {
                "name": "llm-proxy",
                "model": "org-a/shared-suffix",
                "models": {"org-b/shared-suffix": {}},
            }
        ]
        with patch("api.config.cfg", {"custom_providers": custom_cfg}):
            assert _repair_bare_custom_provider_model(
                "shared-suffix", "custom:llm-proxy",
                config_obj={"custom_providers": custom_cfg},
            ) == "org-a/shared-suffix"

    def test_repairs_display_name_provider_via_slug(self):
        from api.routes import _repair_bare_custom_provider_model

        cfg = {
            "custom_providers": [
                {
                    "name": "My Proxy",
                    "models": {"x-ai/grok-composer-2.5-fast": {}},
                }
            ]
        }
        assert _repair_bare_custom_provider_model(
            "grok-composer-2.5-fast",
            "custom:my-proxy",
            config_obj=cfg,
        ) == "x-ai/grok-composer-2.5-fast"

    def test_repairs_list_form_models_catalog(self):
        from api.routes import _repair_bare_custom_provider_model

        cfg = {
            "custom_providers": [
                {
                    "name": "team",
                    "models": [
                        "profile-list/shared",
                        {"id": "profile-list/other"},
                    ],
                }
            ]
        }
        assert _repair_bare_custom_provider_model(
            "shared",
            "custom:team",
            config_obj=cfg,
        ) == "profile-list/shared"

    def test_uses_get_config_when_config_obj_is_none(self):
        from unittest.mock import patch
        from api.routes import _repair_bare_custom_provider_model

        cfg = {
            "custom_providers": [
                {
                    "name": "team",
                    "models": {"vendor/shared": {}},
                }
            ]
        }
        with patch("api.config.get_config") as mock_get_config:
            mock_get_config.return_value = cfg
            result = _repair_bare_custom_provider_model(
                "shared",
                "custom:team",
                config_obj=None,
            )
        assert result == "vendor/shared"
        mock_get_config.assert_called_once_with()

    def test_malformed_custom_provider_name_fails_closed(self):
        from api.routes import _repair_bare_custom_provider_model

        bad_cfg = {
            "custom_providers": [
                {"name": None, "models": {"vendor/ok-model": {}}},
                {"name": "good-proxy", "models": {"vendor/ok-model": {}}},
            ]
        }
        assert _repair_bare_custom_provider_model(
            "ok-model",
            "custom:good-proxy",
            config_obj=bad_cfg,
        ) == "vendor/ok-model"

    def test_profile_config_scoped_not_global_cfg(self):
        from api.routes import _repair_bare_custom_provider_model

        profile_cfg = {
            "custom_providers": [
                {
                    "name": "team",
                    "models": {"profile-vendor/shared": {}},
                }
            ]
        }
        global_cfg = {
            "custom_providers": [
                {
                    "name": "team",
                    "models": {"wrong-vendor/shared": {}},
                }
            ]
        }
        with patch("api.config.cfg", global_cfg):
            assert _repair_bare_custom_provider_model(
                "shared",
                "custom:team",
                config_obj=profile_cfg,
            ) == "profile-vendor/shared"


class TestReadProfileModelConfigWithExplicitProvider:
    def test_returns_profile_default_when_session_has_model_provider(self, tmp_path, monkeypatch):
        from api.routes import _read_profile_model_config

        profile_home = tmp_path / "prof"
        profile_home.mkdir()
        (profile_home / "config.yaml").write_text(
            "model:\n  provider: custom:my-proxy\n  default: x-ai/grok-composer-2.5-fast\n",
            encoding="utf-8",
        )

        class _Session:
            profile = "testprof"

        monkeypatch.setattr(
            "api.profiles.get_hermes_home_for_profile",
            lambda _p: str(profile_home),
        )

        provider, default, _ = _read_profile_model_config(_Session(), "custom:my-proxy")
        assert provider is None
        assert default == "x-ai/grok-composer-2.5-fast"

    def test_returns_none_default_when_session_provider_differs_from_profile(
        self, tmp_path, monkeypatch,
    ):
        from api.routes import _read_profile_model_config

        profile_home = tmp_path / "prof"
        profile_home.mkdir()
        (profile_home / "config.yaml").write_text(
            "model:\n  provider: custom:my-proxy\n  default: x-ai/grok-composer-2.5-fast\n",
            encoding="utf-8",
        )

        class _Session:
            profile = "testprof"

        monkeypatch.setattr(
            "api.profiles.get_hermes_home_for_profile",
            lambda _p: str(profile_home),
        )

        provider, default, _ = _read_profile_model_config(_Session(), "custom:other-proxy")
        assert provider is None
        assert default is None