# Export a tsibble (or plain data frame) to a type-tagged JSON document.
#
# Every cell is encoded as an object so that native types and the distinctions
# between NA, "" (empty string), 0 and FALSE survive the trip through a plain
# scalar HTML form:
#
#   {"t": "integer", "v": "1630"}
#   {"t": "double",  "v": "135.0777", "num": "135.07769999999999"}
#   {"t": "character", "v": ""}            # empty string, NOT NA
#   {"t": "logical", "v": "FALSE"}
#   {"t": "date", "v": "2015-01-01"}
#   {"t": "datetime", "v": "2015-01-01T00:00:00+11:00", "epoch": 1420052400, "tz": "Australia/Melbourne"}
#   {"t": "yearquarter", "v": "1998 Q1"}
#   {"t": "character", "na": true}          # NA, distinct from ""
#
# `v` is always the canonical text that is typed into the form; NA cells have
# no `v` at all and must be entered through the page's per-field NA control.

suppressPackageStartupMessages(library(tsibble))

cell_type <- function(x) {
  cls <- class(x)[1]
  if (inherits(x, "yearquarter")) return("yearquarter")
  if (inherits(x, "yearmonth")) return("yearmonth")
  if (inherits(x, "yearweek")) return("yearweek")
  if (inherits(x, "Date")) return("date")
  if (inherits(x, "POSIXct")) return("datetime")
  if (is.logical(x)) return("logical")
  if (is.integer(x)) return("integer")
  if (is.numeric(x)) return("double")
  "character"
}

fmt_double <- function(x) {
  if (!is.finite(x)) stop("non-finite numeric value cannot be exported: ", x)
  if (x == 0) return("0") # avoid "-0"
  format(x, digits = 15, scientific = FALSE, trim = TRUE)
}

fmt_datetime <- function(x, tz) {
  local <- format(x, "%Y-%m-%dT%H:%M:%S", tz = tz, usetz = FALSE)
  off <- format(x, "%z", tz = tz) # e.g. +1100
  off <- sub("^([+-][0-9]{2})([0-9]{2})$", "\\1:\\2", off)
  paste0(local, off)
}

make_cell <- function(x, i, type, tz) {
  v <- x[[i]]
  if (is.na(v)) return(list(t = type, na = TRUE))
  switch(type,
    character = list(t = type, v = as.character(v)),
    factor = list(t = "character", v = as.character(v)),
    logical = list(t = type, v = if (isTRUE(v)) "TRUE" else "FALSE"),
    integer = list(t = type, v = format(as.integer(v), scientific = FALSE, trim = TRUE)),
    double = list(
      t = type,
      v = fmt_double(as.numeric(v)),
      num = sprintf("%.17g", as.numeric(v))
    ),
    date = list(t = type, v = format(v, "%Y-%m-%d")),
    datetime = list(
      t = type,
      v = fmt_datetime(v, tz),
      epoch = as.numeric(as.POSIXct(v)),
      tz = tz
    ),
    yearquarter = list(t = type, v = format(v)),
    yearmonth = list(t = type, v = format(v)),
    yearweek = list(t = type, v = format(v)),
    stop("unsupported column type: ", type)
  )
}

#' @param df data frame (ideally a tsibble)
#' @param object name of the object (for provenance)
#' @param out output JSON path
#' @param limit 0 = all rows, otherwise first `limit` rows
#' @param pretty pretty-print JSON
export_dataset <- function(df, object, out, limit = 0L, pretty = FALSE) {
  if (!is.data.frame(df)) stop("`df` must be a data frame")
  is_ts <- inherits(df, "tbl_ts")
  if (limit > 0L) df <- utils::head(df, limit)

  key_fields <- if (is_ts) tsibble::key_vars(df) else character()
  index_field <- if (is_ts) tsibble::index_var(df) else NA_character_
  if (!is_ts) {
    stop("a tsibble is required: construct one with as_tsibble() first")
  }
  if (length(key_fields) == 0L) stop("tsibble has no key variables")
  if (is.na(index_field) || !nzchar(index_field)) stop("tsibble has no index variable")

  business_key <- c(key_fields, setdiff(index_field, key_fields))
  classes <- vapply(df, function(x) class(x)[1], character(1))
  fields <- lapply(names(df), function(nm) {
    list(name = nm, type = cell_type(df[[nm]]))
  })

  k <- as.data.frame(df)[business_key]
  if (anyDuplicated(k) != 0L) {
    stop("business key is not unique; refusing to export ambiguous records")
  }

  tz <- ""
  dt_cols <- names(df)[vapply(df, inherits, logical(1), what = "POSIXct")]
  if (length(dt_cols) > 0L) {
    tz <- attr(df[[dt_cols[1]]], "tzone")
    if (is.null(tz) || !nzchar(tz)) tz <- "UTC"
  }

  records <- vector("list", nrow(df))
  for (i in seq_len(nrow(df))) {
    vals <- lapply(names(df), function(nm) {
      make_cell(df[[nm]], i, cell_type(df[[nm]]), tz)
    })
    names(vals) <- names(df)
    records[[i]] <- list(ordinal = i, fields = vals)
  }

  meta <- list(
    name = object,
    object = object,
    class = I(class(df)),
    key_fields = I(key_fields),
    index_field = index_field,
    business_key = I(business_key),
    interval = if (is_ts) format(tsibble::interval(df)) else NA_character_,
    tz = tz,
    fields = I(fields),
    record_count = nrow(df)
  )

  doc <- list(schema = "tsibble-entry-dataset/v1", dataset = meta, records = records)
  json <- jsonlite::toJSON(
    doc,
    auto_unbox = TRUE, null = "null", na = "null",
    digits = NA, pretty = pretty, force = TRUE
  )
  dir.create(dirname(out), recursive = TRUE, showWarnings = FALSE)
  writeLines(json, out, useBytes = TRUE)
  invisible(list(
    out = out,
    records = length(records),
    key_fields = key_fields,
    index_field = index_field,
    business_key = business_key,
    fields = fields
  ))
}
