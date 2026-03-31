import os

TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "55a3ba0fd07d2cd884871536b48dd04f")

PLATFORM_PATHS = {
    "netflix": "/home/abhishek/Claude/netflix/2468198246996321264/CONTENT_INTERACTION/ViewingActivity.csv",
    "prime": None,    # set when data arrives
    "disney": None,
    "hbo": None,
}

DEFAULT_TOP_N = 10
CANDIDATE_POOL_SIZE = 500
MIN_VOTE_COUNT = 100
RECENCY_HALF_LIFE_DAYS = 90
CACHE_DIR = "recommender/cache/tmdb"
