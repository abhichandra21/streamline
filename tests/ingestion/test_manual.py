import os
import tempfile

from recommender.ingestion.manual import parse


def write_tmp(content: str) -> str:
    f = tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False, encoding='utf-8')
    f.write(content)
    f.close()
    return f.name


def test_parses_tv_titles():
    tv = write_tmp("Broadchurch\nHappy Valley\n")
    movies = write_tmp("")
    try:
        events = parse(tv, movies)
        titles = [e.title for e in events]
        assert "Broadchurch" in titles
        assert "Happy Valley" in titles
    finally:
        os.unlink(tv); os.unlink(movies)


def test_parses_movie_titles():
    tv = write_tmp("")
    movies = write_tmp("Zodiac 2007\n1917\n")
    try:
        events = parse(tv, movies)
        titles = [e.title for e in events]
        assert "Zodiac" in titles
        assert "1917" in titles
    finally:
        os.unlink(tv); os.unlink(movies)


def test_strips_year_from_movies():
    tv = write_tmp("")
    movies = write_tmp("12 Strong 2018\n")
    try:
        events = parse(tv, movies)
        assert events[0].title == "12 Strong"
    finally:
        os.unlink(tv); os.unlink(movies)


def test_deduplicates_within_files():
    tv = write_tmp("Broadchurch\nBroadchurch\n")
    movies = write_tmp("Zodiac 2007\nZodiac 2007\n")
    try:
        events = parse(tv, movies)
        titles = [e.title for e in events]
        assert titles.count("Broadchurch") == 1
        assert titles.count("Zodiac") == 1
    finally:
        os.unlink(tv); os.unlink(movies)


def test_tv_content_type():
    tv = write_tmp("Taboo\n")
    movies = write_tmp("")
    try:
        events = parse(tv, movies)
        assert events[0].content_type == 'tv'
        assert events[0].series_name == 'Taboo'
    finally:
        os.unlink(tv); os.unlink(movies)


def test_movie_content_type():
    tv = write_tmp("")
    movies = write_tmp("Inception 2010\n")
    try:
        events = parse(tv, movies)
        assert events[0].content_type == 'movie'
    finally:
        os.unlink(tv); os.unlink(movies)


def test_platform_is_manual():
    tv = write_tmp("Taboo\n")
    movies = write_tmp("")
    try:
        events = parse(tv, movies)
        assert events[0].platform == 'manual'
    finally:
        os.unlink(tv); os.unlink(movies)


def test_skips_blank_lines():
    tv = write_tmp("Taboo\n\n\nOutlander\n")
    movies = write_tmp("")
    try:
        events = parse(tv, movies)
        assert len(events) == 2
    finally:
        os.unlink(tv); os.unlink(movies)


def test_release_year_hint_extracted():
    tv = write_tmp("")
    movies = write_tmp("Honeyland 2019\n")
    try:
        events = parse(tv, movies)
        assert events[0].title == "Honeyland"
        assert events[0].release_year_hint == 2019
    finally:
        os.unlink(tv); os.unlink(movies)


def test_release_year_hint_none_without_year():
    tv = write_tmp("")
    movies = write_tmp("Some Movie\n")
    try:
        events = parse(tv, movies)
        assert events[0].release_year_hint is None
    finally:
        os.unlink(tv); os.unlink(movies)


def test_numeric_title_not_treated_as_year():
    tv = write_tmp("")
    movies = write_tmp("1917\n")
    try:
        events = parse(tv, movies)
        assert events[0].title == "1917"
        assert events[0].release_year_hint is None
    finally:
        os.unlink(tv); os.unlink(movies)


def test_tv_titles_no_year_hint():
    tv = write_tmp("Broadchurch\n")
    movies = write_tmp("")
    try:
        events = parse(tv, movies)
        assert events[0].release_year_hint is None
    finally:
        os.unlink(tv); os.unlink(movies)


def test_language_hint_detected_for_movies():
    tv = write_tmp("")
    movies = write_tmp("डॉन 2006\n")
    try:
        events = parse(tv, movies)
        assert events[0].language_hint == "hi"
    finally:
        os.unlink(tv); os.unlink(movies)


def test_language_hint_detected_for_tv():
    tv = write_tmp("डॉन\n")
    movies = write_tmp("")
    try:
        events = parse(tv, movies)
        assert events[0].language_hint == "hi"
    finally:
        os.unlink(tv); os.unlink(movies)


def test_language_hint_none_for_latin_titles():
    tv = write_tmp("Broadchurch\n")
    movies = write_tmp("Inception 2010\n")
    try:
        events = parse(tv, movies)
        assert all(e.language_hint is None for e in events)
    finally:
        os.unlink(tv); os.unlink(movies)
