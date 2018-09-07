# -*- coding: utf-8 -*-
# Generated by Django 1.11.12 on 2018-09-06 22:22
from __future__ import unicode_literals

from django.db import migrations


def update_content_categories(apps, schema_editor):
    ContentCategory = apps.get_model('web', 'ContentCategory')
    DefaultContentItem = apps.get_model('web', 'DefaultContentItem')
    ContentVisibility = apps.get_model('web', 'ContentVisibility')

    visible = ContentVisibility.objects.get(codename='visible')

    new_tabs = [
        {
            'name': "Home",
            'codename': "home",
        },
        {
            'name': "Get Started",
            'codename': "learn_the_details",
        },
        {
            'name': "My Submissions",
            'codename': "participate",
        },
        {
            'name': "Results",
            'codename': "results",
        }
    ]

    attr_list = ['name', 'codename']

    for index in range(1, 4):
        # Loop through PK's 1-4 and create them if they don't exist
        content_cat, created = ContentCategory.objects.get_or_create(
            pk=index,
        )
        if created:
            content_cat.parent = None
            content_cat.visibility = visible
            content_cat.is_menu = True
            content_cat.content_limit = 1
        for attr in attr_list:
            # Set the new attributes (We explicitly set all so that if we made a new one there is not issues.)
            setattr(content_cat, attr, new_tabs[index-1][attr])
        content_cat.save()
    # Update overview to point to Home category
    overview = DefaultContentItem.objects.get(codename='overview')
    overview.category = ContentCategory.objects.get(codename='home')
    overview.rank = 0
    overview.save()


class Migration(migrations.Migration):

    dependencies = [
        ('web', '0008_auto_20180906_2222'),
    ]

    operations = [
        migrations.RunPython(update_content_categories)
    ]