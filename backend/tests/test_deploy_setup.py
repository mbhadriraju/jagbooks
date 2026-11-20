import pytest
from scripts.deploy_setup import main


def test_setup_validates_without_mutating_database(monkeypatch, capsys):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_PUBLISHABLE_KEY", "publishable")
    main()
    assert "Apply checked-in SQL migrations" in capsys.readouterr().out


def test_setup_rejects_missing_configuration(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="Configure SUPABASE"):
        main()
