#!/usr/bin/env Rscript
# Export a package dataset into the type-tagged JSON document consumed by the
# entry tool.
#
#   Rscript entry-tool/R/export_dataset.R --dataset tourism --limit 5 \
#       --out entry-tool/dataset/tourism.json
#   Rscript entry-tool/R/export_dataset.R --rda data/tourism.rda \
#       --object tourism --out entry-tool/dataset/tourism.json
#
# Run from the repository root (or pass absolute paths).

`%||%` <- function(x, y) if (is.null(x)) y else x

args <- commandArgs(trailingOnly = TRUE)
get_opt <- function(name, default = NULL) {
  hit <- which(args == paste0("--", name))
  if (length(hit) == 0L) return(default)
  if (hit == length(args)) stop("missing value for --", name)
  args[[hit + 1L]]
}
flag <- function(name) any(args == paste0("--", name))

# Locate the repository root from this script's own path, falling back to the
# working directory when that is where the caller ran from.
file_arg <- grep("^--file=", commandArgs(FALSE), value = TRUE)
script_path <- if (length(file_arg) == 1L) normalizePath(sub("^--file=", "", file_arg)) else NA_character_
pkg_root <- if (!is.na(script_path)) {
  normalizePath(file.path(dirname(script_path), "..", ".."), mustWork = FALSE)
} else {
  getwd()
}
if (!file.exists(file.path(pkg_root, "DESCRIPTION"))) pkg_root <- getwd()
if (!file.exists(file.path(pkg_root, "DESCRIPTION"))) {
  stop("cannot locate the tsibble package root (run from the repository root)")
}

source(file.path(pkg_root, "entry-tool", "R", "export_lib.R"))

dataset <- get_opt("dataset")
rda <- get_opt("rda")
object <- get_opt("object")
limit <- as.integer(get_opt("limit", "0"))
out <- get_opt("out")
pretty <- flag("pretty")

if (is.null(out)) stop("--out is required")

if (!is.null(dataset)) {
  env <- new.env(parent = emptyenv())
  utils::data(list = dataset, package = "tsibble", envir = env)
  if (!exists(dataset, envir = env)) stop("dataset not found: ", dataset)
  obj <- get(dataset, envir = env)
  object <- object %||% dataset
} else if (!is.null(rda)) {
  env <- new.env(parent = emptyenv())
  loaded <- load(normalizePath(rda), envir = env)
  if (is.null(object)) {
    if (length(loaded) != 1L) stop("--object is required when the .rda holds multiple objects")
    object <- loaded[[1]]
  }
  if (!exists(object, envir = env)) stop("object not found in .rda: ", object)
  obj <- get(object, envir = env)
} else {
  stop("one of --dataset or --rda is required")
}

info <- export_dataset(obj, object = object, out = out, limit = limit, pretty = pretty)
cat(sprintf(
  "exported %d record(s) of '%s' -> %s\nkey: %s | index: %s\n",
  info$records, object, out,
  paste(info$key_fields, collapse = ", "), info$index_field
))
