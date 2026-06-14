from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_not_required
from django.urls import path

from . import views

# Reset de senha é para quem está deslogado → precisa furar o LoginRequiredMiddleware.
_reset = login_not_required


# Login/logout/signup + fluxo de troca e reset de senha (views nativas do Django).
urlpatterns = [
    path("login/", views.ThrottledLoginView.as_view(), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("signup/", views.signup, name="signup"),

    # Verificação de e-mail (link público; pendente/reenvio exigem login).
    path("verificar/<str:token>/", _reset(views.verificar_email), name="verificar-email"),
    path("verificacao/pendente/", views.verificacao_pendente, name="verificacao-pendente"),
    path("verificacao/reenviar/", views.reenviar_verificacao, name="reenviar-verificacao"),

    # Troca de senha exige estar logado (fica atrás do middleware, ok).
    path("password_change/", auth_views.PasswordChangeView.as_view(), name="password_change"),
    path("password_change/done/", auth_views.PasswordChangeDoneView.as_view(), name="password_change_done"),

    # Reset por e-mail — público.
    path("password_reset/", _reset(auth_views.PasswordResetView.as_view()), name="password_reset"),
    path("password_reset/done/", _reset(auth_views.PasswordResetDoneView.as_view()), name="password_reset_done"),
    path("reset/<uidb64>/<token>/", _reset(auth_views.PasswordResetConfirmView.as_view()), name="password_reset_confirm"),
    path("reset/done/", _reset(auth_views.PasswordResetCompleteView.as_view()), name="password_reset_complete"),
]
