# The test suite wipes every table, so it must refuse any database that is not
# a local *_test one. These tests need no database.

import pytest

from tests.conftest import DEFAULT_TEST_DATABASE_URL, check_test_database_url

USER = "aftercare_dev:aftercare_dev_password"


@pytest.mark.parametrize("url", [
    DEFAULT_TEST_DATABASE_URL,
    f"postgres://{USER}@localhost/aftercare_test",
    f"postgresql://{USER}@127.0.0.1:55432/aftercare_test",
    f"postgresql://{USER}@[::1]:5432/aftercare_test",
    f"postgresql://{USER}@LOCALHOST/other_test",
])
def test_accepts_local_test_databases(url):
    check_test_database_url(url)


@pytest.mark.parametrize("url", [
    f"postgresql://{USER}@localhost:5432/aftercare_dev",
    f"postgresql://{USER}@localhost:5432/aftercare_test_backup",
    f"postgresql://{USER}@localhost:5432/",
    f"postgresql://{USER}@localhost:5432",
    f"postgresql://{USER}@localhost:5432/a/b_test",
])
def test_refuses_wrong_or_missing_database_name(url):
    with pytest.raises(ValueError):
        check_test_database_url(url)


@pytest.mark.parametrize("host", [
    "db.example.com",
    "localhost.example.com",
    "10.0.0.5",
    "0.0.0.0",
    "[::2]",
    "localhost,db.example.com",
    "db.example.com,localhost",
    "localhost,127.0.0.1",
    "db.example.com@localhost",
    "db.example.com:5432@localhost",
])
def test_refuses_anything_but_one_local_host(host):
    with pytest.raises(ValueError):
        check_test_database_url(f"postgresql://{USER}@{host}/aftercare_test")


@pytest.mark.parametrize("query", [
    "?host=db.example.com",
    "?hostaddr=10.0.0.5",
    "?dbname=aftercare_dev",
    "?sslmode=require",
    "?",
])
def test_refuses_any_query_string(query):
    # libpq and asyncpg let a query string override the host and database.
    with pytest.raises(ValueError):
        check_test_database_url(f"postgresql://{USER}@localhost/aftercare_test{query}")


def test_refuses_a_fragment():
    with pytest.raises(ValueError):
        check_test_database_url(f"postgresql://{USER}@localhost/aftercare_test#x")


@pytest.mark.parametrize("scheme", ["mysql", "http", "sqlite", ""])
def test_refuses_non_postgres_scheme(scheme):
    with pytest.raises(ValueError):
        check_test_database_url(f"{scheme}://{USER}@localhost/aftercare_test")


@pytest.mark.parametrize("url", ["", "aftercare_test", "localhost/aftercare_test", "postgresql://localhost:notaport/x_test"])
def test_refuses_garbage(url):
    with pytest.raises(ValueError):
        check_test_database_url(url)
