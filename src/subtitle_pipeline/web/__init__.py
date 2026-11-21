"""Local web console for the subtitle pipeline.

The package binds the console to loopback and keeps the job state under the
work root. The HTTP layer is a thin adapter over the job store and the stage
runner; the pipeline stages and their records stay unchanged.
"""
