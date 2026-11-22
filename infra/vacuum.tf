# Пилосос Dreame (хмара Dreamehome): окрема Lambda з бібліотекою Tasshack/dreame-vacuum (MIT) у шарі.
# Шар збирає власник: tools/build_vacuum_layer.ps1 → infra/build/vacuum_layer/python/…
# Поки шару немає — пилососних ресурсів теж немає (решта TuyaHome працює як була).
# Секрет — лише токен входу в SSM (tools/dreame_login.py); пароль Dreamehome ніде не зберігається.

locals {
  vacuum_ready = fileexists("${path.module}/build/vacuum_layer/python/dreame/__init__.py")
  vacuum_fn    = "${var.name}-vacuum"
  vacuum_param = local.cfg.vacuum.ssm_param
  vacuum_env   = local.vacuum_ready ? { VACUUM_FN = local.vacuum_fn } : {}
}

data "archive_file" "vacuum_layer" {
  count       = local.vacuum_ready ? 1 : 0
  type        = "zip"
  source_dir  = "${path.module}/build/vacuum_layer"
  output_path = "${path.module}/build/vacuum_layer.zip"
  excludes    = ["**/__pycache__/**"]
}

# шар може бути > 50 МБ — пряме завантаження Lambda не приймає, тому через приватний бакет
resource "random_id" "artifacts" {
  count       = local.vacuum_ready ? 1 : 0
  byte_length = 4
}

resource "aws_s3_bucket" "artifacts" {
  count         = local.vacuum_ready ? 1 : 0
  bucket        = "${var.name}-artifacts-${random_id.artifacts[0].hex}"
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "artifacts" {
  count                   = local.vacuum_ready ? 1 : 0
  bucket                  = aws_s3_bucket.artifacts[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_object" "vacuum_layer" {
  count  = local.vacuum_ready ? 1 : 0
  bucket = aws_s3_bucket.artifacts[0].id
  key    = "vacuum_layer.zip"
  source = data.archive_file.vacuum_layer[0].output_path
  etag   = data.archive_file.vacuum_layer[0].output_md5
}

resource "aws_lambda_layer_version" "vacuum" {
  count               = local.vacuum_ready ? 1 : 0
  layer_name          = "${var.name}-dreame"
  s3_bucket           = aws_s3_object.vacuum_layer[0].bucket
  s3_key              = aws_s3_object.vacuum_layer[0].key
  source_code_hash    = data.archive_file.vacuum_layer[0].output_base64sha256
  compatible_runtimes = ["python3.13"]
}

# --- окрема роль: таблиця, свій параметр SSM, свої логи (більше нічого)
resource "aws_iam_role" "vacuum" {
  count              = local.vacuum_ready ? 1 : 0
  name               = "${var.name}-vacuum"
  assume_role_policy = data.aws_iam_policy_document.assume.json
}

data "aws_iam_policy_document" "vacuum" {
  count = local.vacuum_ready ? 1 : 0
  statement {
    actions   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:Query", "dynamodb:UpdateItem", "dynamodb:DeleteItem"]
    resources = [aws_dynamodb_table.main.arn]
  }
  statement {
    actions   = ["ssm:GetParameter", "ssm:PutParameter"]
    resources = ["arn:aws:ssm:${var.region}:${data.aws_caller_identity.me.account_id}:parameter${local.vacuum_param}"]
  }
  statement {
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.vacuum[0].arn}:*"]
  }
}

resource "aws_iam_role_policy" "vacuum" {
  count  = local.vacuum_ready ? 1 : 0
  name   = "${var.name}-vacuum"
  role   = aws_iam_role.vacuum[0].id
  policy = data.aws_iam_policy_document.vacuum[0].json
}

resource "aws_cloudwatch_log_group" "vacuum" {
  count             = local.vacuum_ready ? 1 : 0
  name              = "/aws/lambda/${local.vacuum_fn}"
  retention_in_days = 14
}

resource "aws_lambda_function" "vacuum" {
  count            = local.vacuum_ready ? 1 : 0
  function_name    = local.vacuum_fn
  role             = aws_iam_role.vacuum[0].arn
  runtime          = "python3.13"
  handler          = "vacuum.lambda_handler"
  filename         = data.archive_file.app.output_path
  source_code_hash = data.archive_file.app.output_base64sha256
  layers           = [aws_lambda_layer_version.vacuum[0].arn]
  timeout          = 120
  memory_size      = 1024 # рендер карти (numpy/Pillow) і швидший вхід у хмару Dreame
  environment {
    variables = merge(local.app_env, {
      DREAME_PARAM       = local.vacuum_param
      TELEGRAM_BOT_TOKEN = var.telegram_bot_token
    })
  }
  depends_on = [aws_cloudwatch_log_group.vacuum, aws_iam_role_policy.vacuum]
}
