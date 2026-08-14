"""Normalize matching_id and add a partial unique constraint (ADR 022)."""

from django.db import migrations, models
from django.db.models import Count, Q
from django.db.models.functions import Trim


def find_trimmed_matching_id_collisions(Esim):
    return list(
        Esim.objects.exclude(matching_id="")
        .annotate(trimmed=Trim("matching_id"))
        .exclude(trimmed="")
        .values("trimmed")
        .annotate(n=Count("id"))
        .filter(n__gt=1)
    )


def persist_trimmed_matching_ids(Esim):
    Esim.objects.update(matching_id=Trim("matching_id"))


def normalize_matching_ids(apps, schema_editor):
    Esim = apps.get_model("esims", "Esim")
    collisions = find_trimmed_matching_id_collisions(Esim)
    if collisions:
        raise RuntimeError(
            "esims_esim matching_id trim produced "
            f"{len(collisions)} colliding value(s); refusing unique constraint. "
            "Resolve duplicates manually — do not auto-pick an eSIM."
        )
    persist_trimmed_matching_ids(Esim)


def noop_reverse(apps, schema_editor):
    return None


class Migration(migrations.Migration):

    dependencies = [
        ("esims", "0010_esim_account_ownership"),
    ]

    operations = [
        migrations.RunPython(normalize_matching_ids, noop_reverse),
        migrations.AddConstraint(
            model_name="esim",
            constraint=models.UniqueConstraint(
                condition=~Q(matching_id=""),
                fields=("matching_id",),
                name="esims_esim_matching_id_nonempty_uniq",
            ),
        ),
    ]
