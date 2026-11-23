"""Constants for Anova Precision Oven."""

DOMAIN = "jelly_anova"

MAX_C = 249.0  # oven cap in Celsius
MAX_F = 480  # oven cap in Fahrenheit
DEFAULT_MAX_MINUTES = 180  # default maximum cook time (3 hours)
STALE_SECONDS = 60  # a report older than this is not trusted for stops, checks or cook hours
# Entities stay available this long after an idle report. Since oven firmware 1.4.9 (2026-11) an
# idle oven reports only every few minutes unless the Anova app is open; while cooking it still
# reports often, so a cook keeps the 60 s limit.
IDLE_SHOWN_SECONDS = 600
END_STAGE_TITLE = "jelly-anova-end"  # title of the hidden last stage that ends heating
EVENT_TARGET_REACHED = "jelly_anova_target_reached"
EVENT_COOK_FINISHED = "jelly_anova_cook_finished"
