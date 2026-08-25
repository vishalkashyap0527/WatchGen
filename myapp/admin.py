from django.contrib import admin

from .models import BrowsingHistory, MovieRating, MovieReview, UserInteraction, UserProfile


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'favorite_genre')
    search_fields = ('user__username', 'favorite_genre')


@admin.register(BrowsingHistory)
class BrowsingHistoryAdmin(admin.ModelAdmin):
    list_display = ('user', 'movie_name', 'movie_id', 'viewed_at')
    list_filter = ('viewed_at',)
    search_fields = ('user__username', 'movie_name')


@admin.register(MovieRating)
class MovieRatingAdmin(admin.ModelAdmin):
    list_display = ('user', 'movie_name', 'rating', 'rated_at')
    list_filter = ('rating', 'rated_at')
    search_fields = ('user__username', 'movie_name')


@admin.register(UserInteraction)
class UserInteractionAdmin(admin.ModelAdmin):
    list_display = ('user', 'interaction_type', 'movie_name', 'source_movie', 'created_at')
    list_filter = ('interaction_type', 'created_at')
    search_fields = ('user__username', 'movie_name', 'source_movie')


@admin.register(MovieReview)
class MovieReviewAdmin(admin.ModelAdmin):
    list_display = ('user', 'movie_name', 'sentiment_label', 'sentiment_score', 'updated_at')
    list_filter = ('sentiment_label', 'updated_at')
    search_fields = ('user__username', 'movie_name', 'review_text')
