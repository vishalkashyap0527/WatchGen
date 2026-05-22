import os
import pickle
import time
import logging
from datetime import date
from collections import defaultdict

import requests
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.db.models import Avg, Count, Q
from django.shortcuts import redirect, render
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .models import BrowsingHistory, MovieRating, MovieReview, UserInteraction, UserProfile

# Load data once at startup
movies = pickle.load(open('movie_list.pkl', 'rb'))
similarity = pickle.load(open('similarity.pkl', 'rb'))
TITLE_TO_INDEX = {title: idx for idx, title in enumerate(movies['title'])}
GENRE_TOKEN_MAP = {
    'action': 'Action',
    'adventure': 'Adventure',
    'animation': 'Animation',
    'comedy': 'Comedy',
    'crime': 'Crime',
    'documentary': 'Documentary',
    'drama': 'Drama',
    'family': 'Family',
    'fantasy': 'Fantasy',
    'history': 'History',
    'horror': 'Horror',
    'music': 'Music',
    'mystery': 'Mystery',
    'romance': 'Romance',
    'sciencefiction': 'Science Fiction',
    'thriller': 'Thriller',
    'tvmovie': 'TV Movie',
    'war': 'War',
    'western': 'Western',
}

DEFAULT_POSTER_URL = "/static/myapp/default.svg"
TMDB_ENABLED = os.getenv("TMDB_ENABLED", "1") == "1"
TMDB_COOLDOWN_SECONDS = int(os.getenv("TMDB_COOLDOWN_SECONDS", "300"))

_POSTER_CACHE = {}
_TMDB_FAILURE_COUNT = 0
_TMDB_DISABLED_UNTIL = 0.0
logger = logging.getLogger(__name__)


def _build_tmdb_session():
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
    )
    session = requests.Session()
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


_TMDB_SESSION = _build_tmdb_session()

POSITIVE_WORDS = {
    "amazing", "awesome", "best", "brilliant", "enjoyed", "excellent", "fantastic",
    "good", "great", "impressive", "love", "loved", "nice", "outstanding", "perfect",
    "superb", "wonderful",
}
NEGATIVE_WORDS = {
    "awful", "bad", "boring", "disappointing", "dull", "hate", "hated", "horrible",
    "poor", "terrible", "worst", "waste", "weak",
}


def _normalize_token(value):
    return ''.join(ch for ch in str(value).lower() if ch.isalnum())


def analyze_sentiment(text):
    tokens = [_normalize_token(token) for token in str(text).split()]
    tokens = [token for token in tokens if token]
    if not tokens:
        return 'neutral', 0.0

    positive_hits = sum(1 for token in tokens if token in POSITIVE_WORDS)
    negative_hits = sum(1 for token in tokens if token in NEGATIVE_WORDS)
    score = (positive_hits - negative_hits) / max(len(tokens), 1)

    if score > 0.05:
        label = 'positive'
    elif score < -0.05:
        label = 'negative'
    else:
        label = 'neutral'

    return label, round(score, 3)


def _get_movie_sentiment_summary(movie_name):
    qs = MovieReview.objects.filter(movie_name=movie_name)
    total = qs.count()
    if total == 0:
        return {
            'total_reviews': 0,
            'avg_sentiment_score': 0.0,
            'positive_count': 0,
            'neutral_count': 0,
            'negative_count': 0,
        }

    return {
        'total_reviews': total,
        'avg_sentiment_score': round(qs.aggregate(avg=Avg('sentiment_score'))['avg'] or 0.0, 3),
        'positive_count': qs.filter(sentiment_label='positive').count(),
        'neutral_count': qs.filter(sentiment_label='neutral').count(),
        'negative_count': qs.filter(sentiment_label='negative').count(),
    }


def _extract_movie_genres(movie_name):
    movie_index = TITLE_TO_INDEX.get(movie_name)
    if movie_index is None:
        return []

    tags = str(movies.iloc[movie_index].get('tags', ''))
    genres = []
    for token in tags.split():
        normalized = _normalize_token(token)
        genre_name = GENRE_TOKEN_MAP.get(normalized)
        if genre_name and genre_name not in genres:
            genres.append(genre_name)
    return genres


def _movie_id_for_title(movie_name):
    movie_index = TITLE_TO_INDEX.get(movie_name)
    if movie_index is None:
        return None
    return int(movies.iloc[movie_index].movie_id)


def get_user_preferred_genres(user, top_n=3):
    score_map = defaultdict(float)

    for item in BrowsingHistory.objects.filter(user=user)[:80]:
        for genre_name in _extract_movie_genres(item.movie_name):
            score_map[genre_name] += 1.0

    for item in MovieRating.objects.filter(user=user):
        genres = _extract_movie_genres(item.movie_name)
        if not genres:
            continue

        if item.rating >= 4:
            weight = 1.5 * (item.rating - 2)
        elif item.rating <= 2:
            weight = -1.0
        else:
            weight = 0.5

        for genre_name in genres:
            score_map[genre_name] += weight

    top_genres = sorted(score_map.items(), key=lambda x: x[1], reverse=True)
    top_genres = [(name, round(score, 2)) for name, score in top_genres if score > 0][:top_n]
    return top_genres


def _boost_items_by_genre_preference(items, preferred_genres):
    if not items or not preferred_genres:
        return items

    preferred_set = {name for name, _ in preferred_genres}
    scored = []
    for index, item in enumerate(items):
        genres = set(_extract_movie_genres(item["name"]))
        match_count = len(preferred_set & genres)
        scored.append((match_count, -index, item))

    scored.sort(reverse=True)
    return [item for _, _, item in scored]


def _is_tmdb_available(tmdb_override):
    if tmdb_override is False:
        return False
    if tmdb_override is True:
        return time.time() >= _TMDB_DISABLED_UNTIL
    return TMDB_ENABLED and time.time() >= _TMDB_DISABLED_UNTIL


def fetch_poster(movie_id, tmdb_override=None):
    global _TMDB_FAILURE_COUNT, _TMDB_DISABLED_UNTIL

    cached = _POSTER_CACHE.get(movie_id)
    if cached:
        return cached

    if not _is_tmdb_available(tmdb_override):
        return DEFAULT_POSTER_URL

    api_key = os.getenv("TMDB_API_KEY", "8265bd1679663a7ea12ac168da84d2e8")
    url = f"https://api.themoviedb.org/3/movie/{movie_id}?api_key={api_key}&language=en-US"

    try:
        response = _TMDB_SESSION.get(url, timeout=(2, 5))
        response.raise_for_status()
        data = response.json()
        poster_path = data.get("poster_path")
        if poster_path:
            poster_url = "https://image.tmdb.org/t/p/w500/" + poster_path
            _POSTER_CACHE[movie_id] = poster_url
            _TMDB_FAILURE_COUNT = 0
            return poster_url
    except requests.exceptions.RequestException as e:
        _TMDB_FAILURE_COUNT += 1
        if _TMDB_FAILURE_COUNT >= 3:
            _TMDB_DISABLED_UNTIL = time.time() + TMDB_COOLDOWN_SECONDS
            logger.warning(
                "TMDB temporarily disabled for %s seconds after repeated failures. Last error: %s",
                TMDB_COOLDOWN_SECONDS,
                e,
            )

    return DEFAULT_POSTER_URL


def recommend(movie_name, tmdb_override=None, preferred_genres=None):
    index = TITLE_TO_INDEX.get(movie_name)
    if index is None:
        return []

    distances = sorted(list(enumerate(similarity[index])), reverse=True, key=lambda x: x[1])
    items = []
    for i in distances[1:6]:
        movie_row = movies.iloc[i[0]]
        movie_id = int(movie_row.movie_id)
        items.append(
            {
                "movie_id": movie_id,
                "name": movie_row.title,
                "poster": fetch_poster(movie_id, tmdb_override=tmdb_override),
            }
        )
    return _boost_items_by_genre_preference(items, preferred_genres)


def recommend_with_ratings(user, selected_movie=None, tmdb_override=None, limit=5, preferred_genres=None):
    score_map = defaultdict(float)

    if selected_movie:
        selected_index = TITLE_TO_INDEX.get(selected_movie)
        if selected_index is not None:
            for idx, score in enumerate(similarity[selected_index]):
                if idx == selected_index:
                    continue
                score_map[idx] += float(score)

    user_ratings = MovieRating.objects.filter(user=user)
    for rating_obj in user_ratings:
        rated_index = TITLE_TO_INDEX.get(rating_obj.movie_name)
        if rated_index is None:
            continue

        rating_value = rating_obj.rating
        if rating_value >= 4:
            weight = 0.6 * (rating_value - 3)
        elif rating_value <= 2:
            weight = -0.4 * (3 - rating_value)
        else:
            weight = 0.0

        if weight == 0.0:
            continue

        for idx, score in enumerate(similarity[rated_index]):
            if idx == rated_index:
                continue
            score_map[idx] += weight * float(score)

    rated_movie_names = set(user_ratings.values_list('movie_name', flat=True))
    if selected_movie:
        rated_movie_names.add(selected_movie)

    ranked = sorted(score_map.items(), key=lambda x: x[1], reverse=True)
    items = []

    for idx, score in ranked:
        if score <= 0:
            continue

        movie_row = movies.iloc[idx]
        movie_name = movie_row.title
        if movie_name in rated_movie_names:
            continue

        movie_id = int(movie_row.movie_id)
        items.append(
            {
                "movie_id": movie_id,
                "name": movie_name,
                "poster": fetch_poster(movie_id, tmdb_override=tmdb_override),
            }
        )

        if len(items) >= limit:
            break

    return _boost_items_by_genre_preference(items, preferred_genres)


def _calculate_user_similarity(target_map, other_map):
    common_movies = set(target_map.keys()) & set(other_map.keys())
    if len(common_movies) < 2:
        return 0.0

    agreement_sum = 0.0
    for movie_name in common_movies:
        diff = abs(target_map[movie_name] - other_map[movie_name])
        agreement_sum += max(0.0, 1.0 - (diff / 4.0))

    return agreement_sum / len(common_movies)


def recommend_from_similar_users(user, selected_movie=None, tmdb_override=None, limit=5, preferred_genres=None):
    user_ratings = list(MovieRating.objects.filter(user=user))
    if len(user_ratings) < 2:
        return []

    user_rating_map = {item.movie_name: item.rating for item in user_ratings}
    watched_or_rated = set(user_rating_map.keys())
    if selected_movie:
        watched_or_rated.add(selected_movie)

    other_user_ids = (
        MovieRating.objects
        .exclude(user=user)
        .filter(movie_name__in=user_rating_map.keys())
        .values_list('user_id', flat=True)
        .distinct()
    )

    similar_users = []
    for other_user_id in other_user_ids:
        other_ratings = MovieRating.objects.filter(user_id=other_user_id)
        other_map = {item.movie_name: item.rating for item in other_ratings}
        similarity_score = _calculate_user_similarity(user_rating_map, other_map)
        if similarity_score >= 0.5:
            similar_users.append((other_user_id, similarity_score))

    if not similar_users:
        return []

    score_map = defaultdict(float)
    similar_users = sorted(similar_users, key=lambda x: x[1], reverse=True)[:10]

    for other_user_id, similarity_score in similar_users:
        liked_by_other = MovieRating.objects.filter(user_id=other_user_id, rating__gte=4)
        for item in liked_by_other:
            if item.movie_name in watched_or_rated:
                continue
            score_map[item.movie_name] += similarity_score * item.rating

    if selected_movie in TITLE_TO_INDEX:
        selected_index = TITLE_TO_INDEX[selected_movie]
        for movie_name in list(score_map.keys()):
            idx = TITLE_TO_INDEX.get(movie_name)
            if idx is None:
                continue
            score_map[movie_name] += float(similarity[selected_index][idx])

    ranked = sorted(score_map.items(), key=lambda x: x[1], reverse=True)
    items = []

    for movie_name, score in ranked:
        if score <= 0:
            continue

        movie_index = TITLE_TO_INDEX.get(movie_name)
        if movie_index is None:
            continue

        movie_row = movies.iloc[movie_index]
        movie_id = int(movie_row.movie_id)
        items.append(
            {
                "movie_id": movie_id,
                "name": movie_name,
                "poster": fetch_poster(movie_id, tmdb_override=tmdb_override),
            }
        )
        if len(items) >= limit:
            break

    return _boost_items_by_genre_preference(items, preferred_genres)


def home(request):
    return render(request, 'myapp/home.html')


def about(request):
    return render(request, 'myapp/about.html')


def contact(request):
    return render(request, 'myapp/contact.html')


def _build_period_key(dt_value, period):
    dt_date = dt_value.date()
    if period == 'week':
        iso_year, iso_week, _ = dt_date.isocalendar()
        return f"{iso_year}-W{iso_week:02d}"
    return f"{dt_date.year}-{dt_date.month:02d}"


def _build_trend_series(period='week', limit=12):
    viewed_counts = defaultdict(int)
    rated_counts = defaultdict(int)

    for item in BrowsingHistory.objects.all().only('viewed_at'):
        viewed_counts[_build_period_key(item.viewed_at, period)] += 1

    for item in MovieRating.objects.all().only('rated_at'):
        rated_counts[_build_period_key(item.rated_at, period)] += 1

    all_keys = sorted(set(viewed_counts.keys()) | set(rated_counts.keys()))
    if limit:
        all_keys = all_keys[-limit:]

    trends = []
    for key in all_keys:
        trends.append(
            {
                'period': key,
                'views': viewed_counts.get(key, 0),
                'ratings': rated_counts.get(key, 0),
            }
        )
    return trends


def trending_movies(request):
    most_viewed = (
        BrowsingHistory.objects
        .values('movie_name')
        .annotate(view_count=Count('id'))
        .order_by('-view_count', 'movie_name')[:10]
    )

    top_rated = (
        MovieRating.objects
        .values('movie_name')
        .annotate(avg_rating=Avg('rating'), rating_count=Count('id'))
        .order_by('-avg_rating', '-rating_count', 'movie_name')[:10]
    )

    weekly_trends = _build_trend_series(period='week', limit=24)
    monthly_trends = _build_trend_series(period='month', limit=24)
    weekly_labels = [item['period'] for item in weekly_trends]
    weekly_views = [item['views'] for item in weekly_trends]
    weekly_ratings = [item['ratings'] for item in weekly_trends]
    monthly_labels = [item['period'] for item in monthly_trends]
    monthly_views = [item['views'] for item in monthly_trends]
    monthly_ratings = [item['ratings'] for item in monthly_trends]

    return render(
        request,
        'myapp/trending.html',
        {
            'most_viewed': most_viewed,
            'top_rated': top_rated,
            'weekly_trends': weekly_trends,
            'monthly_trends': monthly_trends,
            'weekly_labels': weekly_labels,
            'weekly_views': weekly_views,
            'weekly_ratings': weekly_ratings,
            'monthly_labels': monthly_labels,
            'monthly_views': monthly_views,
            'monthly_ratings': monthly_ratings,
            'report_date': date.today(),
        },
    )


def sentiment_analysis(request):
    movie_sentiment_rows = (
        MovieReview.objects
        .values('movie_name')
        .annotate(
            avg_sentiment_score=Avg('sentiment_score'),
            review_count=Count('id'),
            positive_count=Count('id', filter=Q(sentiment_label='positive')),
            neutral_count=Count('id', filter=Q(sentiment_label='neutral')),
            negative_count=Count('id', filter=Q(sentiment_label='negative')),
        )
        .order_by('-avg_sentiment_score', '-review_count', 'movie_name')
    )

    return render(
        request,
        'myapp/sentiment.html',
        {
            'movie_sentiments': movie_sentiment_rows,
            'recent_reviews': MovieReview.objects.select_related('user')[:20],
            'report_date': date.today(),
        },
    )


def choosemovie(request):
    tmdb_mode = request.GET.get('tmdb_mode')
    if tmdb_mode in {'auto', 'on', 'off'}:
        if tmdb_mode == 'auto':
            request.session.pop('tmdb_override', None)
        else:
            request.session['tmdb_override'] = (tmdb_mode == 'on')

    tmdb_override = request.session.get('tmdb_override')
    if tmdb_override not in (True, False):
        tmdb_override = None

    selected_movie = request.GET.get('movie')
    movie_names = movies['title'].values
    recommended = []
    tmdb_status_message = ""
    recommendation_note = ""
    recommendation_stage = "none"
    guidance_message = ""
    preferred_genres = []

    if tmdb_override is False or (tmdb_override is None and not TMDB_ENABLED):
        tmdb_status_message = "Posters are disabled in settings. Showing default poster."
    elif time.time() < _TMDB_DISABLED_UNTIL:
        seconds_left = int(_TMDB_DISABLED_UNTIL - time.time())
        if seconds_left < 0:
            seconds_left = 0
        tmdb_status_message = (
            f"Posters unavailable due to network. Retrying automatically in about {seconds_left}s."
        )

    if request.user.is_authenticated:
        preferred_genres = get_user_preferred_genres(request.user)

    if selected_movie and request.user.is_authenticated:
        UserInteraction.objects.create(
            user=request.user,
            movie_name=selected_movie,
            movie_id=_movie_id_for_title(selected_movie),
            interaction_type='browse',
        )
        recommended = recommend_from_similar_users(
            user=request.user,
            selected_movie=selected_movie,
            tmdb_override=tmdb_override,
            preferred_genres=preferred_genres,
        )
        if recommended:
            recommendation_note = "Collaborative filtering active: recommendations from similar users."
            recommendation_stage = "collaborative"
        else:
            recommended = recommend_with_ratings(
                user=request.user,
                selected_movie=selected_movie,
                tmdb_override=tmdb_override,
                preferred_genres=preferred_genres,
            )
            if recommended:
                recommendation_note = "Personalized using your ratings + selected movie."
                recommendation_stage = "ratings"
            else:
                recommended = recommend(
                    selected_movie,
                    tmdb_override=tmdb_override,
                    preferred_genres=preferred_genres,
                )
                recommendation_note = "No strong rating signal yet, showing normal recommendations."
                recommendation_stage = "content"
    elif selected_movie:
        recommended = recommend(selected_movie, tmdb_override=tmdb_override)
        if recommended:
            recommendation_stage = "content"
    elif request.user.is_authenticated:
        recommended = recommend_from_similar_users(
            user=request.user,
            selected_movie=None,
            tmdb_override=tmdb_override,
            preferred_genres=preferred_genres,
        )
        if recommended:
            recommendation_note = "Collaborative filtering active: recommendations from similar users."
            recommendation_stage = "collaborative"
        else:
            recommended = recommend_with_ratings(
                user=request.user,
                selected_movie=None,
                tmdb_override=tmdb_override,
                preferred_genres=preferred_genres,
            )
            if recommended:
                recommendation_note = "Personalized from movies you rated highly."
                recommendation_stage = "ratings"

    if request.user.is_authenticated and not recommended:
        guidance_message = "Recommendations not ready yet. Rate at least 2 movies, then open recommendations again."
    elif not request.user.is_authenticated and not selected_movie:
        guidance_message = "Choose a movie and click Show Recommendation to see results."
    elif not recommended:
        guidance_message = "No recommendations found for this selection. Please try another movie."

    tmdb_mode_value = "auto"
    if tmdb_override is True:
        tmdb_mode_value = "on"
    elif tmdb_override is False:
        tmdb_mode_value = "off"

    return render(
        request,
        'myapp/choosemovie.html',
        {
            'movies': movie_names,
            'selected_movie': selected_movie,
            'recommended': recommended,
            'tmdb_mode': tmdb_mode_value,
            'tmdb_status_message': tmdb_status_message,
            'recommendation_note': recommendation_note,
            'recommendation_stage': recommendation_stage,
            'guidance_message': guidance_message,
            'preferred_genres': preferred_genres,
        },
    )


# Signup
def signup_view(request):
    if request.method == 'POST':
        username = request.POST.get('username', '').strip()
        email = request.POST.get('email', '').strip()
        password = request.POST.get('password', '')

        if not username or not email or not password:
            messages.error(request, "All fields are required")
            return redirect('signup')

        if len(password) < 8:
            messages.error(request, "Password must be at least 8 characters")
            return redirect('signup')

        if User.objects.filter(username__iexact=username).exists():
            messages.error(request, "Username already exists")
            return redirect('signup')

        if User.objects.filter(email__iexact=email).exists():
            messages.error(request, "Email already registered")
            return redirect('signup')

        user = User.objects.create_user(
            username=username,
            email=email,
            password=password,
        )
        login(request, user)
        return redirect('home')

    return render(request, 'myapp/signup.html')


# Login
def login_view(request):
    if request.method == 'POST':
        username = request.POST.get('username', '').strip()
        password = request.POST.get('password', '')

        user = authenticate(request, username=username, password=password)

        if user is not None:
            login(request, user)
            return redirect('home')

        messages.error(request, "Invalid Credentials")
        return redirect('login')

    return render(request, 'myapp/login.html')


# Logout
def logout_view(request):
    logout(request)
    return redirect('login')


# Profile Page
@login_required
def profile_view(request):
    profile, _ = UserProfile.objects.get_or_create(user=request.user)

    if request.method == 'POST':
        profile.favorite_genre = request.POST.get('favorite_genre', '').strip()
        if 'profile_picture' in request.FILES:
            profile.profile_picture = request.FILES['profile_picture']
        profile.save()
        messages.success(request, "Profile updated successfully")
        return redirect('profile')

    history_qs = BrowsingHistory.objects.filter(user=request.user)
    ratings_qs = MovieRating.objects.filter(user=request.user)
    history = history_qs[:20]
    ratings = ratings_qs[:20]
    interactions = UserInteraction.objects.filter(user=request.user)
    preferred_genres = get_user_preferred_genres(request.user, top_n=5)
    reviews_qs = MovieReview.objects.filter(user=request.user)

    context = {
        'profile': profile,
        'history': history,
        'ratings': ratings,
        'reviews': reviews_qs[:20],
        'preferred_genres': preferred_genres,
        'analytics': {
            'total_views': history_qs.count(),
            'total_ratings': ratings_qs.count(),
            'total_reviews': reviews_qs.count(),
            'total_clicks': interactions.filter(interaction_type='click').count(),
            'total_browses': interactions.filter(interaction_type='browse').count(),
            'last_interaction': interactions.first(),
        },
    }

    return render(request, 'myapp/profile.html', context)


# Movie Detail (History Save)
def movie_detail(request, movie_name, movie_id=None):
    if movie_id is None:
        movie_id_value = request.GET.get('movie_id')
        if movie_id_value and movie_id_value.isdigit():
            movie_id = int(movie_id_value)

    if request.user.is_authenticated:
        if request.GET.get('from_reco') == '1':
            UserInteraction.objects.create(
                user=request.user,
                movie_name=movie_name,
                movie_id=movie_id,
                interaction_type='click',
                source_movie=request.GET.get('source', ''),
            )

        UserInteraction.objects.create(
            user=request.user,
            movie_name=movie_name,
            movie_id=movie_id,
            interaction_type='view',
            source_movie=request.GET.get('source', ''),
        )

        history, created = BrowsingHistory.objects.get_or_create(
            user=request.user,
            movie_name=movie_name,
            defaults={'movie_id': movie_id},
        )

        if not created:
            history.save()  # updates viewed_at

        if request.method == 'POST':
            rating_value = request.POST.get('rating')
            review_text = request.POST.get('review_text', '').strip()
            has_success = False

            if rating_value in {'1', '2', '3', '4', '5'}:
                MovieRating.objects.update_or_create(
                    user=request.user,
                    movie_name=movie_name,
                    defaults={
                        'movie_id': movie_id,
                        'rating': int(rating_value),
                    },
                )
                UserInteraction.objects.create(
                    user=request.user,
                    movie_name=movie_name,
                    movie_id=movie_id,
                    interaction_type='rate',
                    source_movie=request.GET.get('source', ''),
                )
                has_success = True

            if review_text:
                sentiment_label, sentiment_score = analyze_sentiment(review_text)
                MovieReview.objects.update_or_create(
                    user=request.user,
                    movie_name=movie_name,
                    defaults={
                        'movie_id': movie_id,
                        'review_text': review_text,
                        'sentiment_label': sentiment_label,
                        'sentiment_score': sentiment_score,
                    },
                )
                has_success = True

            if has_success:
                messages.success(request, "Your feedback was saved.")
                return redirect('movie_detail', movie_name=movie_name)

            messages.error(request, "Please provide rating (1-5) or a review.")

    current_rating = None
    current_review = None
    movie_sentiment_summary = _get_movie_sentiment_summary(movie_name)
    if request.user.is_authenticated:
        existing_rating = MovieRating.objects.filter(
            user=request.user,
            movie_name=movie_name,
        ).first()
        if existing_rating:
            current_rating = existing_rating.rating

        current_review = MovieReview.objects.filter(
            user=request.user,
            movie_name=movie_name,
        ).first()

    return render(
        request,
        'myapp/movie_detail.html',
        {
            'movie_name': movie_name,
            'current_rating': current_rating,
            'current_review': current_review,
            'movie_sentiment_summary': movie_sentiment_summary,
        },
    )
