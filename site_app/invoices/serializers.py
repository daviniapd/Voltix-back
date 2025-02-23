# import mimetypes
# from rest_framework import serializers

# class InvoiceUploadSerializer(serializers.Serializer):

#     file = serializers.FileField()

#     def validate_file(self, value):

#         max_size = 5 * 1024 * 1024  #5mb
#         if value.size > max_size:
#             raise serializers.ValidationError("File size exceeds 5 MB.")

#         allowed_types = ['application/pdf', 'image/png', 'image/jpeg']
#         if value.content_type not in allowed_types:
#             raise serializers.ValidationError(
#                 f"Invalid file type: {value.content_type}. Allowed types are PDF, PNG, and JPEG."
#             )
        
#         ext = mimetypes.guess_extension(value.content_type)
#         if ext and not value.name.endswith(ext):
#             raise serializers.ValidationError(
#                 f"File extension does not match content type: Expected {ext}"
#             )

#         return value




    

# ONLY PDF VERSION
import mimetypes
from rest_framework import serializers

class InvoiceSerializer(serializers.Serializer):
    invoice_id = serializers.IntegerField()
    user_id = serializers.IntegerField()
    upload_date = serializers.DateTimeField()
    amount_due = serializers.DecimalField(max_digits=10, decimal_places=2)
    due_date = serializers.DateField()
    provider = serializers.CharField(max_length=150)
    file_path = serializers.CharField(max_length=255)
    ocr_data = serializers.JSONField()

class InvoiceUploadSerializer(serializers.Serializer):

    file = serializers.FileField()

    def validate_file(self, value):

        max_size = 5 * 1024 * 1024  # 5 MB in bytes
        if value.size > max_size:
            raise serializers.ValidationError("File size exceeds 5 MB.")

        if value.content_type != 'application/pdf':
            raise serializers.ValidationError(
                f"Invalid file type: {value.content_type}. Only PDF files are allowed."
            )

        ext = mimetypes.guess_extension(value.content_type)
        if not ext or ext != '.pdf':
            raise serializers.ValidationError(
                "File extension does not match content type. Only '.pdf' files are allowed."
            )

        if not value.name.lower().endswith('.pdf'):
            raise serializers.ValidationError("File name must end with '.pdf'.")

        return value

from voltix.models import Invoice
class InvoiceSerializer(serializers.ModelSerializer):
    comparison_status = serializers.CharField(read_only=True)  # Add estado as a read-only field

    class Meta:
        model = Invoice
        fields = '__all__'