import os

TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "55a3ba0fd07d2cd884871536b48dd04f")

PLATFORM_PATHS = {
    "netflix": os.path.join(os.path.dirname(__file__), "data/netflix/2468198246996321264/CONTENT_INTERACTION/ViewingActivity.csv"),
    "prime": os.path.join(os.path.dirname(__file__), "data/prime_video/Your Prime Video Viewing Activity/Viewing History.csv"),
    "disney": None,
    "hbo": None,
}

DEFAULT_TOP_N = 10
CANDIDATE_POOL_SIZE = 500
MIN_VOTE_COUNT = 100
RECENCY_HALF_LIFE_DAYS = 90
CACHE_DIR = "recommender/cache/tmdb"
