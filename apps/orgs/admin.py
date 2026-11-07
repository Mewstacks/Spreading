from django.contrib import admin

from .models import ContainerLink, Destination, Organization

admin.site.register(Organization)
admin.site.register(Destination)
admin.site.register(ContainerLink)
