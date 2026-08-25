from django.db import models
from django.contrib.auth.models import User
# Create your models here.

class UserProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    favorite_genre = models.CharField(max_length=100, blank=True)
    profile_picture = models.ImageField(upload_to='profiles/', blank=True, null=True)

    def __str__(self):
        return self.user.username


class BrowsingHistory(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    movie_id = models.IntegerField(null=True, blank=True)
    movie_name = models.CharField(max_length=255)
    viewed_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-viewed_at']

    def __str__(self):
        return f"{self.user.username} - {self.movie_name}"


class MovieRating(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    movie_id = models.IntegerField(null=True, blank=True)
    movie_name = models.CharField(max_length=255)
    rating = models.PositiveSmallIntegerField(default=3)
    rated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-rated_at']
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'movie_name'],
                name='unique_user_movie_rating',
            ),
        ]

    def __str__(self):
        return f"{self.user.username} rated {self.movie_name} ({self.rating}/5)"


class UserInteraction(models.Model):
    INTERACTION_CHOICES = [
        ('browse', 'Browse'),
        ('click', 'Click'),
        ('view', 'View'),
        ('rate', 'Rate'),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE)
    movie_id = models.IntegerField(null=True, blank=True)
    movie_name = models.CharField(max_length=255)
    interaction_type = models.CharField(max_length=20, choices=INTERACTION_CHOICES)
    source_movie = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.user.username} {self.interaction_type} {self.movie_name}"


class MovieReview(models.Model):
    SENTIMENT_CHOICES = [
        ('positive', 'Positive'),
        ('neutral', 'Neutral'),
        ('negative', 'Negative'),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE)
    movie_id = models.IntegerField(null=True, blank=True)
    movie_name = models.CharField(max_length=255)
    review_text = models.TextField()
    sentiment_label = models.CharField(max_length=10, choices=SENTIMENT_CHOICES, default='neutral')
    sentiment_score = models.FloatField(default=0.0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'movie_name'],
                name='unique_user_movie_review',
            ),
        ]

    def __str__(self):
        return f"{self.user.username} review {self.movie_name} ({self.sentiment_label})"
