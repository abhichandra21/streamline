from recommender.models import Recommendation


def test_recommendation_fields():
    rec = Recommendation(
        title="Broadchurch",
        content_type="tv",
        score=0.91,
        vote_average=8.4,
        genres=["Crime", "Drama"],
        explanation="Fits your love of slow-burn British crime with strong characters.",
    )
    assert rec.title == "Broadchurch"
    assert rec.content_type == "tv"
    assert rec.score == 0.91
    assert rec.vote_average == 8.4
    assert rec.genres == ["Crime", "Drama"]
    assert "British" in rec.explanation


def test_recommendation_is_dataclass():
    from dataclasses import fields
    field_names = {f.name for f in fields(Recommendation)}
    assert field_names == {"title", "content_type", "score", "vote_average", "genres", "explanation"}
