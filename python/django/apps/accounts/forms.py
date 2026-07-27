from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import UserCreationForm

User = get_user_model()


class SignUpForm(UserCreationForm):
    """Cadastro com e-mail obrigatório e único. Senha validada pelos
    AUTH_PASSWORD_VALIDATORS (força mínima vem de lá, não daqui)."""

    email = forms.EmailField(
        required=True,
        widget=forms.EmailInput(attrs={"autocomplete": "email", "placeholder": "voce@email.com"}),
    )

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("username", "email")

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        # iexact evita duplicar conta variando só a caixa do e-mail.
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("Já existe conta com este e-mail.")
        return email

    def save(self, commit=True):
        user = super().save(commit=False)
        user.email = self.cleaned_data["email"]
        if commit:
            user.save()
        return user
