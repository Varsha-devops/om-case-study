provider "local" {}

# Define files using for_each
locals {
  files = {
    file1 = "This is file 1"
     #file2 is removed
    file3 = "This is file 3"
    file4 = "This is file 4"
    file5 = "This is file 5"
  }
}

resource "local_file" "files" {
  for_each = local.files

  filename = "${each.key}.txt"
  content  = each.value
}
