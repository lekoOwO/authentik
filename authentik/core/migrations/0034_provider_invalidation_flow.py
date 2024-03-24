# Generated by Django 5.0.3 on 2024-03-24 12:52

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("authentik_core", "0033_alter_user_options"),
        ("authentik_flows", "0027_auto_20231028_1424"),
    ]

    operations = [
        migrations.AddField(
            model_name="provider",
            name="invalidation_flow",
            field=models.ForeignKey(
                default=None,
                help_text="Flow used ending the session from a provider.",
                null=True,
                on_delete=django.db.models.deletion.SET_DEFAULT,
                related_name="provider_invalidation",
                to="authentik_flows.flow",
            ),
        ),
    ]