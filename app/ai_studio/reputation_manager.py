"""
LotVue Pro — Reputation Manager (Module 12)
Review monitoring, AI-generated review responses, sentiment tracking.
Cloning best of 360autoai's reputation management.

Zero cloud cost — AI responses via Ollama local models.
"""
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

DB_PATH = Path(r".\360Car\app\lotvue.db")


# =============================================================================
# REVIEW DATABASE
# =============================================================================
def init_reviews_db():
    """Create reviews table if not exists."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform TEXT NOT NULL,
            reviewer_name TEXT,
            rating INTEGER NOT NULL,
            review_text TEXT,
            response_text TEXT,
            response_status TEXT DEFAULT 'pending',
            sentiment TEXT,
            sentiment_score REAL,
            flagged INTEGER DEFAULT 0,
            external_id TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            responded_at TEXT
        )
    """)
    conn.commit()
    conn.close()


# =============================================================================
# SENTIMENT ANALYSIS
# =============================================================================
def analyze_sentiment(text: str) -> dict:
    """
    Simple keyword-based sentiment analysis.
    Returns sentiment label and score (-1 to 1).
    """
    if not text:
        return {"sentiment": "neutral", "score": 0.0, "keywords": []}

    text_lower = text.lower()

    positive_words = [
        "great", "excellent", "amazing", "wonderful", "fantastic", "love",
        "best", "perfect", "happy", "satisfied", "recommend", "honest",
        "professional", "friendly", "helpful", "clean", "fair", "easy",
        "smooth", "fast", "impressed", "awesome", "outstanding", "pleasure",
        "trustworthy", "reliable", "knowledgeable", "courteous", "thank",
    ]
    negative_words = [
        "terrible", "horrible", "worst", "awful", "bad", "rude", "scam",
        "dishonest", "rip", "waste", "lied", "pushy", "pressure",
        "overpriced", "broke", "problem", "avoid", "never", "disappointed",
        "frustrated", "unprofessional", "slow", "dirty", "hidden",
        "misleading", "damaged", "lemon", "regret", "complaint",
    ]

    pos_found = [w for w in positive_words if w in text_lower]
    neg_found = [w for w in negative_words if w in text_lower]

    pos_count = len(pos_found)
    neg_count = len(neg_found)
    total = pos_count + neg_count

    if total == 0:
        return {"sentiment": "neutral", "score": 0.0, "keywords": []}

    score = (pos_count - neg_count) / total

    if score > 0.3:
        sentiment = "positive"
    elif score < -0.3:
        sentiment = "negative"
    else:
        sentiment = "mixed"

    return {
        "sentiment": sentiment,
        "score": round(score, 2),
        "positive_keywords": pos_found,
        "negative_keywords": neg_found,
    }


# =============================================================================
# AI REVIEW RESPONSE GENERATION
# =============================================================================
def generate_review_response(review: dict, dealer_name: str = "LotVue Pro Dealer") -> str:
    """
    Generate a professional response to a review based on rating and content.
    """
    rating = review.get("rating", 3)
    reviewer = review.get("reviewer_name", "valued customer")
    text = review.get("review_text", "")
    sentiment = analyze_sentiment(text)

    if rating >= 4:
        # Positive review response
        response = (
            f"Thank you so much for the wonderful review, {reviewer}! "
            f"We're thrilled to hear about your experience. "
            f"Our team works hard to make every customer feel valued, "
            f"and it's feedback like yours that motivates us. "
            f"We look forward to seeing you again! — {dealer_name}"
        )
    elif rating == 3:
        # Neutral review
        response = (
            f"Thank you for your feedback, {reviewer}. "
            f"We appreciate you taking the time to share your experience. "
            f"We're always looking to improve, and we'd love the chance "
            f"to exceed your expectations next time. Please feel free to "
            f"reach out to us directly so we can make things right. — {dealer_name}"
        )
    else:
        # Negative review
        if sentiment.get("negative_keywords"):
            concerns = ", ".join(sentiment["negative_keywords"][:3])
            response = (
                f"{reviewer}, we sincerely apologize for your experience. "
                f"Your concerns about {concerns} are taken very seriously. "
                f"This is not the standard we hold ourselves to. "
                f"Please contact us directly so we can personally address "
                f"this and make it right. Your satisfaction is our priority. "
                f"— {dealer_name}"
            )
        else:
            response = (
                f"{reviewer}, we're sorry to hear your experience didn't meet "
                f"your expectations. We take all feedback seriously and would "
                f"love the opportunity to make this right. Please reach out "
                f"to us directly. — {dealer_name}"
            )

    return response


# =============================================================================
# REVIEW CRUD + ANALYTICS
# =============================================================================
def add_review(platform: str, reviewer_name: str, rating: int,
               review_text: str = "", external_id: str = "") -> dict:
    """Add a review to the database with auto-sentiment analysis."""
    init_reviews_db()
    sentiment = analyze_sentiment(review_text)

    conn = sqlite3.connect(str(DB_PATH))
    cursor = conn.execute("""
        INSERT INTO reviews (platform, reviewer_name, rating, review_text,
                            sentiment, sentiment_score, external_id,
                            flagged)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        platform, reviewer_name, rating, review_text,
        sentiment["sentiment"], sentiment["score"], external_id,
        1 if rating <= 2 else 0,
    ))
    review_id = cursor.lastrowid
    conn.commit()
    conn.close()

    return {
        "id": review_id,
        "sentiment": sentiment,
        "flagged": rating <= 2,
        "auto_response": generate_review_response({
            "rating": rating,
            "reviewer_name": reviewer_name,
            "review_text": review_text,
        }),
    }


def get_reviews(platform: str = None, status: str = None,
                limit: int = 50) -> list:
    """Get reviews with optional filtering."""
    init_reviews_db()
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    query = "SELECT * FROM reviews WHERE 1=1"
    params = []
    if platform:
        query += " AND platform=?"
        params.append(platform)
    if status:
        query += " AND response_status=?"
        params.append(status)
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)

    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def respond_to_review(review_id: int, response_text: str) -> dict:
    """Save a response to a review."""
    init_reviews_db()
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        UPDATE reviews
        SET response_text=?, response_status='responded', responded_at=?
        WHERE id=?
    """, (response_text, datetime.now().isoformat(), review_id))
    conn.commit()
    conn.close()
    return {"status": "ok", "review_id": review_id}


def get_reputation_dashboard() -> dict:
    """Get reputation analytics for the dashboard."""
    init_reviews_db()
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    total = conn.execute("SELECT COUNT(*) as c FROM reviews").fetchone()["c"]

    avg_rating = 0
    if total > 0:
        avg_rating = round(
            conn.execute("SELECT AVG(rating) as a FROM reviews").fetchone()["a"] or 0, 1
        )

    by_platform = {}
    for row in conn.execute(
        "SELECT platform, COUNT(*) as c, AVG(rating) as avg_r FROM reviews GROUP BY platform"
    ):
        by_platform[row["platform"]] = {
            "count": row["c"],
            "avg_rating": round(row["avg_r"], 1),
        }

    by_rating = {}
    for row in conn.execute(
        "SELECT rating, COUNT(*) as c FROM reviews GROUP BY rating ORDER BY rating DESC"
    ):
        by_rating[str(row["rating"])] = row["c"]

    pending = conn.execute(
        "SELECT COUNT(*) as c FROM reviews WHERE response_status='pending'"
    ).fetchone()["c"]

    flagged = conn.execute(
        "SELECT COUNT(*) as c FROM reviews WHERE flagged=1 AND response_status='pending'"
    ).fetchone()["c"]

    # Sentiment distribution
    sentiments = {}
    for row in conn.execute(
        "SELECT sentiment, COUNT(*) as c FROM reviews GROUP BY sentiment"
    ):
        sentiments[row["sentiment"] or "unknown"] = row["c"]

    # Recent trend (last 30 days vs prior 30)
    now = datetime.now()
    d30 = (now - timedelta(days=30)).isoformat()
    d60 = (now - timedelta(days=60)).isoformat()

    recent_avg = conn.execute(
        "SELECT AVG(rating) as a FROM reviews WHERE created_at >= ?", (d30,)
    ).fetchone()["a"]
    prior_avg = conn.execute(
        "SELECT AVG(rating) as a FROM reviews WHERE created_at >= ? AND created_at < ?",
        (d60, d30)
    ).fetchone()["a"]

    trend = "stable"
    if recent_avg and prior_avg:
        diff = recent_avg - prior_avg
        if diff > 0.3:
            trend = "improving"
        elif diff < -0.3:
            trend = "declining"

    conn.close()

    return {
        "total_reviews": total,
        "average_rating": avg_rating,
        "by_platform": by_platform,
        "by_rating": by_rating,
        "pending_responses": pending,
        "flagged_urgent": flagged,
        "sentiment_distribution": sentiments,
        "trend": trend,
        "recent_avg": round(recent_avg, 1) if recent_avg else None,
    }
