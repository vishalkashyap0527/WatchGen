from datetime import datetime, timezone as dt_timezone

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from .models import BrowsingHistory, MovieRating, MovieReview
from .views import analyze_sentiment, movies


class RatingRecommendationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='tester',
            email='tester@example.com',
            password='testpass123',
        )

    def test_movie_rating_saved_and_updated(self):
        self.client.login(username='tester', password='testpass123')
        movie_name = movies.iloc[0].title
        detail_url = reverse('movie_detail', kwargs={'movie_name': movie_name})

        self.client.post(detail_url, {'rating': '5'})
        rating = MovieRating.objects.get(user=self.user, movie_name=movie_name)
        self.assertEqual(rating.rating, 5)

        self.client.post(detail_url, {'rating': '2'})
        rating.refresh_from_db()
        self.assertEqual(rating.rating, 2)

    def test_choosemovie_shows_personalized_note_when_ratings_exist(self):
        self.client.login(username='tester', password='testpass123')

        seed_movie = movies.iloc[0].title
        MovieRating.objects.create(
            user=self.user,
            movie_name=seed_movie,
            movie_id=int(movies.iloc[0].movie_id),
            rating=5,
        )

        response = self.client.get(
            reverse('choosemovie'),
            {'movie': seed_movie, 'tmdb_mode': 'off'},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Personalized using your ratings + selected movie.")

    def test_choosemovie_uses_collaborative_filtering_from_similar_users(self):
        self.client.login(username='tester', password='testpass123')

        movie_a = movies.iloc[0]
        movie_b = movies.iloc[1]
        movie_c = movies.iloc[2]

        MovieRating.objects.create(
            user=self.user,
            movie_name=movie_a.title,
            movie_id=int(movie_a.movie_id),
            rating=5,
        )
        MovieRating.objects.create(
            user=self.user,
            movie_name=movie_b.title,
            movie_id=int(movie_b.movie_id),
            rating=4,
        )

        similar_user = User.objects.create_user(
            username='similar_user',
            email='similar@example.com',
            password='testpass123',
        )
        MovieRating.objects.create(
            user=similar_user,
            movie_name=movie_a.title,
            movie_id=int(movie_a.movie_id),
            rating=5,
        )
        MovieRating.objects.create(
            user=similar_user,
            movie_name=movie_b.title,
            movie_id=int(movie_b.movie_id),
            rating=4,
        )
        MovieRating.objects.create(
            user=similar_user,
            movie_name=movie_c.title,
            movie_id=int(movie_c.movie_id),
            rating=5,
        )

        response = self.client.get(
            reverse('choosemovie'),
            {'movie': movie_a.title, 'tmdb_mode': 'off'},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Collaborative filtering active: recommendations from similar users.")
        self.assertContains(response, movie_c.title)


class TrendingMoviesTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='trend_user',
            email='trend@example.com',
            password='testpass123',
        )
        self.movie_a = movies.iloc[0]
        self.movie_b = movies.iloc[1]

    def test_trending_page_shows_top_lists(self):
        BrowsingHistory.objects.create(
            user=self.user,
            movie_name=self.movie_a.title,
            movie_id=int(self.movie_a.movie_id),
        )
        BrowsingHistory.objects.create(
            user=self.user,
            movie_name=self.movie_a.title,
            movie_id=int(self.movie_a.movie_id),
        )
        BrowsingHistory.objects.create(
            user=self.user,
            movie_name=self.movie_b.title,
            movie_id=int(self.movie_b.movie_id),
        )

        MovieRating.objects.create(
            user=self.user,
            movie_name=self.movie_a.title,
            movie_id=int(self.movie_a.movie_id),
            rating=5,
        )
        MovieRating.objects.create(
            user=self.user,
            movie_name=self.movie_b.title,
            movie_id=int(self.movie_b.movie_id),
            rating=3,
        )

        response = self.client.get(reverse('trending_movies'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.movie_a.title)
        self.assertContains(response, "Most Viewed Movies")
        self.assertContains(response, "Top Rated Movies")

    def test_trending_page_shows_weekly_and_monthly_periods(self):
        history = BrowsingHistory.objects.create(
            user=self.user,
            movie_name=self.movie_a.title,
            movie_id=int(self.movie_a.movie_id),
        )
        rating = MovieRating.objects.create(
            user=self.user,
            movie_name=self.movie_a.title,
            movie_id=int(self.movie_a.movie_id),
            rating=4,
        )

        fixed_dt = datetime(2026, 2, 10, 10, 0, tzinfo=dt_timezone.utc)
        BrowsingHistory.objects.filter(id=history.id).update(viewed_at=fixed_dt)
        MovieRating.objects.filter(id=rating.id).update(rated_at=fixed_dt)

        response = self.client.get(reverse('trending_movies'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "2026-W07")
        self.assertContains(response, "2026-02")


class SentimentAnalysisTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='review_user',
            email='review@example.com',
            password='testpass123',
        )
        self.movie = movies.iloc[0]

    def test_analyze_sentiment_labels(self):
        self.assertEqual(analyze_sentiment("Amazing excellent movie")[0], 'positive')
        self.assertEqual(analyze_sentiment("Worst boring movie")[0], 'negative')
        self.assertEqual(analyze_sentiment("Movie was okay")[0], 'neutral')

    def test_sentiment_analysis_page_shows_movie_scores(self):
        MovieReview.objects.create(
            user=self.user,
            movie_name=self.movie.title,
            movie_id=int(self.movie.movie_id),
            review_text='Amazing and excellent experience',
            sentiment_label='positive',
            sentiment_score=0.4,
        )
        MovieReview.objects.create(
            user=User.objects.create_user('review_user_2', 'review2@example.com', 'testpass123'),
            movie_name=self.movie.title,
            movie_id=int(self.movie.movie_id),
            review_text='bad and boring',
            sentiment_label='negative',
            sentiment_score=-0.4,
        )

        response = self.client.get(reverse('sentiment_analysis'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Sentiment Score Per Movie")
        self.assertContains(response, self.movie.title)
        self.assertContains(response, "0.000")
