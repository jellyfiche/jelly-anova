"""Constants for Anova Precision Oven."""

DOMAIN = "jelly_anova"

MAX_C = 249.0  # oven cap in Celsius
MAX_F = 480  # oven cap in Fahrenheit
DEFAULT_MAX_MINUTES = 180  # default maximum cook time (3 hours)
STALE_SECONDS = 60  # entities go unavailable after this long with no report
END_STAGE_TITLE = "jelly-anova-end"  # title of the hidden last stage that ends heating
EVENT_TARGET_REACHED = "jelly_anova_target_reached"
EVENT_COOK_FINISHED = "jelly_anova_cook_finished"
