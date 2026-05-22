
from django.urls import path
from .import views

urlpatterns = [

    path('',views.home,name='home'),
path('about/',views.about,name='about'),
path('contact/',views.contact,name='contact'),
path('choosemovie/',views.choosemovie,name='choosemovie'),
path('trending/',views.trending_movies,name='trending_movies'),
path('sentiment/',views.sentiment_analysis,name='sentiment_analysis'),
path('signup/', views.signup_view, name='signup'),
    path('login/', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),
    path('profile/', views.profile_view, name='profile'),

    # Movie detail route (important)
    path('movie/<path:movie_name>/', views.movie_detail, name='movie_detail'),
]
