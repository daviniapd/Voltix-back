################################################################################################################################
############################## PROCESO DE TRATAMIENTO DE PDF + OCR TO JSON + JSON PARA BASE DE DATOS ###########################
################################################################################################################################

import os
import cv2
import pytesseract
import logging
import fitz  # PyMuPDF
from PIL import Image
import numpy as np
from django.conf import settings
from rest_framework.views import APIView
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from drf_yasg.utils import swagger_auto_schema
from drf_yasg import openapi
from .serializers import InvoiceUploadSerializer
from voltix.models import Invoice
from voltix.utils.upload_cloudinary import process_and_upload_image
from io import BytesIO
from django.core.files.uploadedfile import InMemoryUploadedFile



logger = logging.getLogger(__name__)

class InvoiceProcessView(APIView):
    permission_classes = [IsAuthenticated]
    parser_classes = (MultiPartParser, FormParser)

    @swagger_auto_schema(
        operation_summary="Subir y procesar facturas en PDF (OCR incluido)",
        operation_description=(
            "Permite a un usuario autenticado subir un archivo PDF, "
            "convertirlo en imágenes, procesarlas (escalado a grises) y ejecutar OCR para extraer texto."
        ),
        manual_parameters=[
            openapi.Parameter(
                name="file",
                in_=openapi.IN_FORM,
                type=openapi.TYPE_FILE,
                description="El archivo PDF a subir y procesar.",
            ),
        ],
        responses={
            201: openapi.Response(
                description="Archivo procesado y texto extraído exitosamente.",
                examples={
                    "application/json": {
                        "status": "success",
                        "message": "Archivo procesado y texto extraído exitosamente.",
                        "ocr_text": "Texto extraído del archivo...",
                    }
                },
            ),
            400: openapi.Response(
                description="Error en la validación del archivo.",
                examples={
                    "application/json": {
                        "status": "error",
                        "details": {
                            "file": [
                                "This field is required.",
                                "File size exceeds 5 MB.",
                                "Invalid file type: application/msword. Only PDF files are allowed.",
                                "File extension does not match content type. Only '.pdf' files are allowed.",
                                "File name must end with '.pdf'."
                            ],
                        }
                    }
                },
            ),
            500: openapi.Response(
                description="Error durante el procesamiento.",
                examples={
                    "application/json": {
                        "status": "error",
                        "message": "Error durante el procesamiento.",
                        "details": "Detalles del error...",
                    }
                },
            ),
        },
    )
    def post(self, request, *args, **kwargs):
        serializer = InvoiceUploadSerializer(data=request.data)
        if serializer.is_valid():
            try:
                uploaded_file = serializer.validated_data["file"]

                # Crear carpeta temporal si no existe
                temp_folder = settings.FILE_UPLOAD_TEMP_DIR or os.path.join(settings.BASE_DIR, "media", "temp")
                os.makedirs(temp_folder, exist_ok=True)

                # Guardar archivo temporalmente
                file_path = os.path.join(temp_folder, uploaded_file.name)
                with open(file_path, "wb+") as destination:
                    for chunk in uploaded_file.chunks():
                        destination.write(chunk)

                logger.info(f"Archivo '{uploaded_file.name}' subido exitosamente a {file_path}.")

                # Convertir PDF a imágenes
                images = self.pdf_to_images(file_path)
                
                # Subir la primera página a Cloudinary
                if images:
                    first_page_image = images[0]  # Usar solo la primera página
                   

                    # Convertir la imagen original (sin procesar) a un formato compatible con Cloudinary
                    original_image = Image.open(BytesIO(first_page_image))  # Convertir bytes a PIL Image
                    image_io = BytesIO()
                    original_image.save(image_io, format='PNG')  # Guardar como PNG en un flujo en memoria
                    image_io.seek(0)

                    # Crear un InMemoryUploadedFile para subir a Cloudinary
                    processed_photo = InMemoryUploadedFile(
                        image_io,  # Archivo en memoria
                        field_name='ImageField',  # Nombre del campo
                        name='processed_image.png',  # Nombre del archivo
                        content_type='image/png',  # Tipo MIME
                        size=image_io.tell(),  # Tamaño del archivo
                        charset=None  # Charset, None para imágenes
                    )

                    try:
                        photo_url = process_and_upload_image(processed_photo, folder="invoices")
                    except Exception as cloudinary_error:
                        logger.error(f"Error al subir la imagen a Cloudinary: {str(cloudinary_error)}")
                        return Response(
                            {"status": "error", "message": "Error al subir la imagen.", "details": str(cloudinary_error)},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                        )
                else:
                    photo_url = None  # Si no hay imágenes, no habrá URL asociada

                # Procesar las dos primeras páginas (si existen)
                processed_images = []
                ocr_text_combined = ""
                for idx, img_data in enumerate(images[:2]):  # Tomar solo la primera y segunda página
                    grayscale_image = self.process_image(img_data)
                    processed_images.append(grayscale_image)

                    # Realizar OCR en la imagen procesada
                    ocr_text = self.perform_ocr(grayscale_image)
                    ocr_text_combined += ocr_text + "\n"  # Combinar texto de todas las páginas

                # Convertir OCR a JSON
                parsed_data = self.convert_ocr_to_json(ocr_text_combined)

                # Guardar los datos en la base de datos
                if "error" not in parsed_data:
                    try:
                        Invoice.objects.create(
                            user=request.user,  # Relacionar la factura con el usuario autenticado
                            billing_period_start=parsed_data["periodo_facturacion"].get("inicio"),
                            billing_period_end=parsed_data["periodo_facturacion"].get("fin"),
                            data=parsed_data,  # Guardar todo el JSON en el campo 'data'
                            image_url=photo_url,  # Guardar la URL de la primera página en el modelo
                        )

                        logger.info("Factura guardada exitosamente en la base de datos.")
                    except Exception as db_error:
                        logger.error(f"Error al guardar en la base de datos: {str(db_error)}")
                        return Response(
                            {
                                "status": "error",
                                "message": "Error al guardar los datos en la base de datos.",
                                "details": str(db_error),
                            },
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                        )

                # Eliminar el archivo PDF subido
                if os.path.exists(file_path):
                    os.remove(file_path)
                    logger.info(f"Archivo PDF '{uploaded_file.name}' eliminado.")

                # Devolver el texto crudo del OCR y el JSON procesado
                return Response(
                    {
                        "status": "success",
                        "message": "Archivo procesado y texto extraído exitosamente.",
                        "ocr_text": ocr_text_combined,  # Texto crudo capturado por OCR
                        "parsed_data": parsed_data,  # JSON estructurado a partir del texto OCR
                    },
                    status=status.HTTP_201_CREATED,
                )
            except Exception as e:
                logger.error(f"Error durante el procesamiento: {str(e)}")
                return Response(
                    {
                        "status": "error",
                        "message": "Error durante el procesamiento.",
                        "details": str(e),
                    },
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )

        logger.warning(f"Validación fallida: {serializer.errors}")
        return Response({"status": "error", "details": serializer.errors}, status=status.HTTP_400_BAD_REQUEST)

    def pdf_to_images(self, pdf_path):
        """
        Convierte un archivo PDF en imágenes (una por página).
        """
        images = []
        try:
            pdf_document = fitz.open(pdf_path)
            for page_number in range(len(pdf_document)):
                page = pdf_document[page_number]
                zoom_x, zoom_y = 2.0, 2.0  # 2x scaling for higher DPI
                matrix = fitz.Matrix(zoom_x, zoom_y)
                pix = page.get_pixmap(matrix=matrix)
                image_data = pix.tobytes("png")
                images.append(image_data)
            pdf_document.close()
            return images
        except Exception as e:
            logger.error(f"Error al convertir PDF a imágenes: {str(e)}")
            return []

    def process_image(self, image_data):
        """
        Convierte una imagen a escala de grises usando OpenCV.
        """
        try:
            np_array = np.frombuffer(image_data, np.uint8)
            image = cv2.imdecode(np_array, cv2.IMREAD_COLOR)
            grayscale_image = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

            # Aumentar contraste usando CLAHE
            clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
            contrast_image = clahe.apply(grayscale_image)

            # Eliminar ruido
            denoised_image = cv2.fastNlMeansDenoising(contrast_image, None, 30, 7, 21)

            # Afilar la imagen
            kernel = np.array([[0, -1, 0],
                               [-1, 5, -1],
                               [0, -1, 0]])
            sharpened_image = cv2.filter2D(denoised_image, -1, kernel)

            return sharpened_image
        except Exception as e:
            logger.error(f"Error durante el procesamiento de la imagen: {str(e)}")
            return None

    def perform_ocr(self, image):
        """
        Realiza OCR en una imagen usando Tesseract y retorna el texto extraído.
        """
        try:
            custom_oem_psm_config = r"--oem 3 --psm 11"
            pytesseract.pytesseract.tesseract_cmd = r"/usr/bin/tesseract"

            pil_image = Image.fromarray(image)

            ocr_result = pytesseract.image_to_string(pil_image, lang="spa", config=custom_oem_psm_config)

            return ocr_result

        except Exception as e:
            logger.error(f"Error al realizar OCR: {str(e)}")
            return ""

    def convert_ocr_to_json(self, ocr_text):
        """
        Convierte el texto OCR extraído a un JSON según la comercializadora detectada.
        """
        try:
            if "endesa" in ocr_text.lower():
                return self.extract_endesa_data(ocr_text)
            elif "iberdrola" in ocr_text.lower():
                return self.extract_iberdrola_data(ocr_text)
            elif "lidera comercializadora energia" in ocr_text.lower():
                return self.extract_lidera_data(ocr_text)
            elif "naturgy iberia" in ocr_text.lower():
                return self.extract_naturgy_data(ocr_text)
            elif "e-distribución" in ocr_text.lower():
                return self.extract_edistribucion_data(ocr_text)
            else:
                return {"error": "No se reconoció ninguna comercializadora en el OCR."}

        except Exception as e:
            logger.error(f"Error al convertir OCR a JSON: {str(e)}")
            return {"error": "Error al convertir OCR a JSON."}

    def extract_endesa_data(self, ocr_text):
        """
        Extrae los datos específicos de las facturas de Endesa a partir del texto OCR.
        """
        try:
            import re
            from datetime import datetime

            # Normalizar el texto para facilitar la búsqueda
            normalized_text = re.sub(r"\s+", " ", ocr_text)  # Reemplazar múltiples espacios o saltos de línea con un solo espacio

            # Función auxiliar para convertir fechas al formato YYYY-MM-DD
            def format_date_to_yyyy_mm_dd(date_str):
                try:
                    return datetime.strptime(date_str, "%d/%m/%Y").strftime("%Y-%m-%d")
                except ValueError:
                    return None

            # Función auxiliar para convertir fechas con nombres de meses
            def format_date_with_month_name(date_match):
                meses = {
                    "enero": "01", "febrero": "02", "marzo": "03", "abril": "04",
                    "mayo": "05", "junio": "06", "julio": "07", "agosto": "08",
                    "septiembre": "09", "octubre": "10", "noviembre": "11", "diciembre": "12"
                }
                try:
                    dia, mes_texto, anio = date_match.groups()
                    mes = meses.get(mes_texto.lower())
                    if mes:
                        return f"{anio}-{mes}-{dia.zfill(2)}"
                except (ValueError, AttributeError):
                    pass
                return None

            # Extraer "nombre_cliente"
            nombre_cliente_match = re.search(r"Titular del contrato:\s*(.*?)\s*\n\nCUPS:", ocr_text, re.IGNORECASE)
            nombre_cliente = nombre_cliente_match.group(1).strip() if nombre_cliente_match else None

            # Extraer "numero_referencia"
            numero_referencia_match = re.search(r"Referencia:\s*([\w\/-]+)", normalized_text)
            numero_referencia = numero_referencia_match.group(1).strip() if numero_referencia_match else None

            # Extraer "fecha_emision"
            fecha_emision_match = re.search(r"Fecha emisión factur[a|:]*\s*(\d{2}/\d{2}/\d{4})", normalized_text, re.IGNORECASE)
            fecha_emision = format_date_to_yyyy_mm_dd(fecha_emision_match.group(1)) if fecha_emision_match else None


            # Extraer "periodo_inicio"
            periodo_inicio_match = re.search(r"Periodo de facturación: del\s*(\d{2}/\d{2}/\d{4})", normalized_text)
            periodo_inicio = format_date_to_yyyy_mm_dd(periodo_inicio_match.group(1)) if periodo_inicio_match else None

            # Extraer "periodo_fin"
            periodo_fin_match = re.search(r"a\s*(\d{2}/\d{2}/\d{4})", normalized_text)
            periodo_fin = format_date_to_yyyy_mm_dd(periodo_fin_match.group(1)) if periodo_fin_match else None

            # Extraer "dias"
            dias_match = re.search(r"\((\d+)\s*días\)", normalized_text)
            dias = int(dias_match.group(1)) if dias_match else None

            # Extraer "fecha_cargo"
            fecha_cargo_match = re.search(r"Fecha de cargo:\s*(\d{2})\s*de\s*(\w+)\s*de\s*(\d{4})", normalized_text)
            fecha_cargo = format_date_with_month_name(fecha_cargo_match) if fecha_cargo_match else None

            # Extraer "mandato"
            mandato_match = re.search(r"Cod\.?Mandato:\s*(\w+)", normalized_text)
            mandato = mandato_match.group(1).strip() if mandato_match else None

            # Extraer "costo_potencia"
            costo_potencia_match = re.search(r"Potencia.*? (\d{1,3}(?:\.\d{3})*,\d{2}) €", normalized_text)
            costo_potencia = float(costo_potencia_match.group(1).replace(".", "").replace(",", ".")) if costo_potencia_match else None

            # Extraer "costo_energia"
            costo_energia_match = re.search(r"Energía\s+(\d{1,3}(?:\.\d{3})*,\d{2})", normalized_text)
            costo_energia = float(costo_energia_match.group(1).replace(".", "").replace(",", ".")) if costo_energia_match else None

            # Extraer "descuentos"
            descuentos_match = re.search(r"Descuentos.*? (-?\d{1,3}(?:\.\d{3})*,\d{2}) €", normalized_text)
            descuentos = float(descuentos_match.group(1).replace(".", "").replace(",", ".")) if descuentos_match else None

            # Extraer "impuestos"
            impuestos_match = re.search(r"Impuestos.*? (\d{1,3}(?:\.\d{3})*,\d{2}) €", normalized_text)
            impuestos = float(impuestos_match.group(1).replace(".", "").replace(",", ".")) if impuestos_match else None

            # Extraer "total_a_pagar"
            total_a_pagar_match = re.search(r"Total.*? (\d{1,3}(?:\.\d{3})*,\d{2}) €", normalized_text)
            total_a_pagar = float(total_a_pagar_match.group(1).replace(".", "").replace(",", ".")) if total_a_pagar_match else None

            # Extraer "consumo_punta"
            try:
                llano_position = normalized_text.lower().find("llano")
                if llano_position != -1:
                    text_before_llano = normalized_text[:llano_position]
                    numeros_antes_de_llano = re.findall(r"(\d{1,3}(?:\.\d{3})*,\d{2})", text_before_llano)
                    consumo_punta = float(numeros_antes_de_llano[-1].replace(".", "").replace(",", ".")) if numeros_antes_de_llano else None
                else:
                    consumo_punta = None
            except Exception as e:
                logger.error(f"Error al extraer consumo_punta: {str(e)}")
                consumo_punta = None

            # Extraer "consumo_valle"
            try:
                potencia_positions = [m.start() for m in re.finditer(r"potencia", normalized_text.lower())]
                if len(potencia_positions) >= 6:
                    text_before_sixth_potencia = normalized_text[:potencia_positions[5]]
                    numeros_antes_de_potencia = re.findall(r"(\d{1,3}(?:\.\d{3})*,\d{2})", text_before_sixth_potencia)
                    consumo_valle = float(numeros_antes_de_potencia[-3].replace(".", "").replace(",", ".")) if len(numeros_antes_de_potencia) >= 3 else None
                else:
                    consumo_valle = None
            except Exception as e:
                logger.error(f"Error al extraer consumo_valle: {str(e)}")
                consumo_valle = None

            # Extraer "consumo_total"
            import re

            ocr_text = """
            Consumo Total

            1,112,537 kWh
            """

            # Buscar el valor después de "Consumo Total"
            consumo_total_match = re.search(r"Consumo Total\s*\n\n\s*(\d{1,3}(?:,\d{3})+)", ocr_text)

            # Procesar el valor encontrado
            if consumo_total_match:
                consumo_total = consumo_total_match.group(1).replace(",", "")  # Quitar comas
                consumo_total = float(consumo_total[:-3] + '.' + consumo_total[-3:])  # Insertar el punto y convertir a float
            else:
                consumo_total = None  # Si no se encuentra el valor

            # Resultado para el JSON
            resultado_json = {
                "consumo_total": consumo_total
            }

            print(resultado_json)

            # Extraer "precio_efectivo_energia"
            precio_efectivo_energia_match = re.search(r"ha salido a\s*([\d,\.]+) €/kWh", normalized_text)
            precio_efectivo_energia = float(precio_efectivo_energia_match.group(1).replace(",", ".")) if precio_efectivo_energia_match else None

            # Extraer "forma de pago"
            forma_pago_match = re.search(r"Forma de pago:\s*([^\d\n]*)", normalized_text, re.IGNORECASE)
            forma_pago = forma_pago_match.group(1).strip() if forma_pago_match else None

            # Construir JSON
            parsed_data = {
                "nombre_cliente": nombre_cliente,
                "numero_referencia": numero_referencia,
                "fecha_emision": fecha_emision,
                "periodo_facturacion": {
                    "inicio": periodo_inicio,
                    "fin": periodo_fin,
                    "dias": dias,
                },
                "forma_pago": forma_pago,
                "fecha_cargo": fecha_cargo,
                "mandato": mandato,
                "desglose_cargos": {
                    "costo_potencia": costo_potencia,
                    "costo_energia": costo_energia,
                    "descuentos": descuentos,
                    "impuestos": impuestos,
                    "total_a_pagar": total_a_pagar,
                },
                "detalles_consumo": {
                    "consumo_punta": consumo_punta,
                    "consumo_valle": consumo_valle,
                    "consumo_total": consumo_total,
                    "precio_efectivo_energia": precio_efectivo_energia,
                },
            }

            return parsed_data

        except Exception as e:
            logger.error(f"Error al convertir OCR a JSON para Endesa: {str(e)}")
            return {"error": "Error al convertir OCR a JSON para Endesa."}

    def extract_iberdrola_data(self, ocr_text):
        """
        Extrae los datos específicos de las facturas de Iberdrola a partir del texto OCR.
        """
        try:
            import re
            from datetime import datetime

            # Mapeo de meses en español a números
            meses = {
                "enero": "01", "febrero": "02", "marzo": "03", "abril": "04",
                "mayo": "05", "junio": "06", "julio": "07", "agosto": "08",
                "septiembre": "09", "octubre": "10", "noviembre": "11", "diciembre": "12"
            }

            # Inicializar valores por defecto
            nombre_cliente = None
            numero_referencia = None
            fecha_emision = None
            periodo_inicio = None
            periodo_fin = None
            dias = None
            forma_pago = None
            fecha_cargo = None
            mandato = None
            costo_potencia = None
            costo_energia = None
            descuentos = None
            impuestos = None
            total_a_pagar = None
            consumo_punta = None
            consumo_valle = None
            consumo_total = None
            precio_efectivo_energia = None

            # Extraer "nombre_cliente"
            nombre_cliente_match = re.search(r"(?:\n)([A-Z\s]+)\n.*Potencia punta", ocr_text)
            nombre_cliente = nombre_cliente_match.group(1).strip() if nombre_cliente_match else None

            # Extraer "numero_referencia"
            numero_referencia_match = re.search(r"N\* DE CONTRATO:\s*([\d]+)", ocr_text)
            numero_referencia = numero_referencia_match.group(1).strip() if numero_referencia_match else None

            # Extraer "fecha_emision"
            fecha_emision_match = re.search(
                r"FECHA DE EMISIÓN:.*?\n\n.*?\n\n(\d{1,2})\s+de\s+(\w+)\s+de\s+(\d{4})", ocr_text, re.DOTALL
            )
            if fecha_emision_match:
                dia, mes_texto, anio = fecha_emision_match.groups()
                mes_numero = meses.get(mes_texto.lower())
                if mes_numero:
                    fecha_emision = f"{anio}-{mes_numero}-{dia.zfill(2)}"


            # Extraer "periodo_facturacion"
            periodo_match = re.search(
                r"PERIODO DE FACTURACIÓN.*?\n\n(\d{1,2}/\d{1,2}/\d{4})\s+(\d{1,2}/\d{1,2}/\d{4})", ocr_text, re.DOTALL
            )
            if periodo_match:
                try:
                    # Convertir las fechas al formato YYYY-MM-DD
                    periodo_inicio = datetime.strptime(periodo_match.group(1), "%d/%m/%Y").strftime("%Y-%m-%d")
                    periodo_fin = datetime.strptime(periodo_match.group(2), "%d/%m/%Y").strftime("%Y-%m-%d")
                except ValueError:
                    periodo_inicio, periodo_fin = None, None


            # Extraer "dias"
            dias_match = re.search(r"FECHA DE EMISIÓN:.*?\n\n.*?\n\n(\d+)", ocr_text, re.DOTALL)
            dias = int(dias_match.group(1)) if dias_match else None


            # Extraer "forma_pago"
            forma_pago_match = re.search(r"Forma de pago\s*([^\n]+)", ocr_text)
            forma_pago = forma_pago_match.group(1).strip() if forma_pago_match else None

            # Extraer "fecha_cargo"
            fecha_cargo_match = re.search(r"FECHA PREVISTA DE COBRO:\s*(\d{2}/\d{2}/\d{4})", ocr_text)
            if fecha_cargo_match:
                try:
                    fecha_cargo = datetime.strptime(fecha_cargo_match.group(1), "%d/%m/%Y").strftime("%Y-%m-%d")
                except ValueError:
                    fecha_cargo = None

            # Extraer "mandato"
            mandato_match = re.search(r"Codigo de mandato\s*([\d]+)", ocr_text)
            mandato = mandato_match.group(1).strip() if mandato_match else None

            # Extraer el valor de "costo_punta" (antes de "€\n\nValle")
            costo_punta_match = re.search(r"([\d,\.]+)\s*€\s*\n\n\s*Valle", ocr_text)
            if costo_punta_match:
                try:
                    costo_punta_raw = costo_punta_match.group(1)
                    costo_punta = float(costo_punta_raw.replace(",", ".")) / 100  # Mover el punto 2 posiciones a la izquierda
                except ValueError:
                    costo_punta = 0.0  # Valor por defecto si el formato no es válido
            else:
                costo_punta = 0.0  # Valor por defecto si no se encuentra el patrón

            # Extraer el valor de "costo_valle" (antes de " €\n\nTotal importe potencia")
            costo_valle_match = re.search(r"([\d,\.]+)\s*€\s*\n\n\s*Total importe potencia", ocr_text)
            if costo_valle_match:
                try:
                    costo_valle_raw = costo_valle_match.group(1)
                    costo_valle = float(costo_valle_raw.replace(",", ".")) / 100  # Mover el punto 2 posiciones a la izquierda
                except ValueError:
                    costo_valle = 0.0  # Valor por defecto si el formato no es válido
            else:
                costo_valle = 0.0  # Valor por defecto si no se encuentra el patrón

            # Calcular el valor total de "costo_potencia" y limitar a 2 decimales
            costo_potencia = round(costo_punta + costo_valle, 2)



            # Extraer valor de "costo_energia" (antes de " €\n\nEnergia consumida")
            costo_energia_match = re.search(r"([\d,\.]+)\s*€\s*\n\n\s*Energia consumida", ocr_text)
            if costo_energia_match:
                try:
                    costo_energia_raw = costo_energia_match.group(1)
                    costo_energia = float(costo_energia_raw.replace(",", "."))
                except ValueError:
                    costo_energia = 0.0  # Valor por defecto si el formato no es válido
            else:
                costo_energia = 0.0  # Valor por defecto si no se encuentra el patrón


            # Extraer "descuentos"
            descuentos_match = re.search(r"Descuentos.*?(-?\d{1,3},\d{2}) €", ocr_text)
            descuentos = float(descuentos_match.group(1).replace(",", ".")) if descuentos_match else None

            # Extraer "impuestos"
            # Extraer primer valor de "impuestos" (antes de " €\n\nTOTAL ENERGÍA")
            impuestos_valor1_match = re.search(r"([\d,\.]+)\s*€\s*\n\n\s*TOTAL ENERGÍA", ocr_text)
            if impuestos_valor1_match:
                try:
                    impuestos_valor1_raw = impuestos_valor1_match.group(1)
                    impuestos_valor1 = float(impuestos_valor1_raw.replace(",", "."))
                except ValueError:
                    impuestos_valor1 = 0.0  # Valor por defecto si el formato no es válido
            else:
                impuestos_valor1 = 0.0  # Valor por defecto si no se encuentra el patrón


            # Extraer segundo valor de "impuestos" (antes de "TOTAL IMPORTE FACTURA")
            impuestos_valor2_match = re.search(r"([\d,\.]+)\s*€\s*\n\n\s*TOTAL IMPORTE FACTURA", ocr_text)
            if impuestos_valor2_match:
                try:
                    impuestos_valor2_raw = impuestos_valor2_match.group(1)
                    impuestos_valor2 = float(impuestos_valor2_raw.replace(",", "."))
                except ValueError:
                    impuestos_valor2 = 0.0  # Valor por defecto si el formato no es válido
            else:
                impuestos_valor2 = 0.0  # Valor por defecto si no se encuentra el patrón

            # Calcular el valor total de "impuestos"
            impuestos = impuestos_valor1 + impuestos_valor2



            # Extraer "total_a_pagar"
            total_a_pagar_match = re.search(r"TOTAL IMPORTE FACTURA\s*\n\n\s*([\d,\.]+)\s*€", ocr_text)
            if total_a_pagar_match:
                try:
                    total_a_pagar_raw = total_a_pagar_match.group(1)
                    total_a_pagar = float(total_a_pagar_raw.replace(",", "."))
                except ValueError:
                    total_a_pagar = 0.0  # Valor por defecto si el formato no es válido
            else:
                total_a_pagar = 0.0  # Valor por defecto si no se encuentra el patrón


            # Extraer "consumo_punta"
            try:
                # Buscar el valor después de "desagregados han sido punta:"
                consumo_punta_match = re.search(r"desagregados han sido punta:\s*([\d,\.]+)\s*kWh", ocr_text, re.IGNORECASE)
                
                if consumo_punta_match:
                    # Extraer el valor y convertirlo en float
                    consumo_punta = float(consumo_punta_match.group(1).replace(",", "").replace(".", "."))
                    consumo_punta = round(consumo_punta, 2)  # Asegurar dos decimales
                else:
                    consumo_punta = 0.00  # Valor por defecto si no se encuentra el patrón
            except Exception as e:
                logger.error(f"Error al extraer 'consumo_punta': {str(e)}")
                consumo_punta = 0.00  # Valor por defecto en caso de error


            # Extraer "consumo_valle"
            try:
                # Buscar el contexto específico de "Las potencias máximas demandadas"
                consumo_valle_match = re.search(
                    r"([\d,\.]+)\s*kWh,\s*\n=4\s*\nLas potencias máximas demandadas", ocr_text, re.IGNORECASE
                )

                if consumo_valle_match:
                    # Extraer el valor encontrado y convertirlo en formato decimal
                    consumo_valle = float(consumo_valle_match.group(1).replace(",", "."))
                else:
                    consumo_valle = 0.00  # Valor predeterminado si no se encuentra
            except Exception as e:
                logger.error(f"Error al extraer 'consumo_valle': {str(e)}")
                consumo_valle = 0.00  # Valor predeterminado en caso de error

            # Extraer "consumo_total"
            consumo_total_match = re.search(r"(\d{1,3},\d{2})\s*kWh", ocr_text)
            if consumo_total_match:
                consumo_total = float(consumo_total_match.group(1).replace(",", "."))
            else:
                consumo_total = 0

            # Extraer "precio_efectivo_energia"
            precio_efectivo_energia_match = re.search(r"([\d,\.]+)\s*€/kWh", ocr_text)
            if precio_efectivo_energia_match:
                precio_efectivo_energia_raw = precio_efectivo_energia_match.group(1)
                # Normalizar el valor reemplazando posibles errores como comas y puntos
                try:
                    precio_efectivo_energia = float(precio_efectivo_energia_raw.replace(",", "."))
                except ValueError:
                    # Si el formato no es válido, asignamos 0 como predeterminado
                    precio_efectivo_energia = 0
            else:
                precio_efectivo_energia = 0

            # Construir JSON con los valores extraídos
            parsed_data = {
                "nombre_cliente": nombre_cliente,
                "numero_referencia": numero_referencia,
                "fecha_emision": fecha_emision,
                "periodo_facturacion": {
                    "inicio": periodo_inicio,
                    "fin": periodo_fin,
                    "dias": dias,
                },
                "forma_pago": forma_pago,
                "fecha_cargo": fecha_cargo,
                "mandato": mandato,
                "desglose_cargos": {
                    "costo_potencia": costo_potencia,
                    "costo_energia": costo_energia,
                    "descuentos": descuentos if descuentos else 0,
                    "impuestos": impuestos,
                    "total_a_pagar": total_a_pagar,
                },
                "detalles_consumo": {
                    "consumo_punta": consumo_punta,
                    "consumo_valle": consumo_valle,
                    "consumo_total": consumo_total,
                    "precio_efectivo_energia": precio_efectivo_energia,
                },
            }

            return parsed_data

        except Exception as e:
            logger.error(f"Error al convertir OCR a JSON para Iberdrola: {str(e)}")
            return {
                "error": "Error al convertir OCR a JSON para Iberdrola."
            }

    def extract_lidera_data(self, ocr_text):
        """
        Extrae los datos específicos de las facturas de Lidera Energia a partir del texto OCR.
        """
        try:
            import re
            from datetime import datetime

            # Mapeo de meses en español a números
            meses = {
                "enero": "01", "febrero": "02", "marzo": "03", "abril": "04",
                "mayo": "05", "junio": "06", "julio": "07", "agosto": "08",
                "septiembre": "09", "octubre": "10", "noviembre": "11", "diciembre": "12"
            }

            # Inicializar valores por defecto
            nombre_cliente = None
            numero_referencia = None
            fecha_emision = None
            periodo_inicio = None
            periodo_fin = None
            dias = None
            forma_pago = None
            fecha_cargo = None
            mandato = None
            costo_potencia = None
            costo_energia = None
            descuentos = None
            impuestos = None
            total_a_pagar = None
            consumo_punta = None
            consumo_valle = None
            consumo_total = None
            precio_efectivo_energia = None

            # Extraer "nombre_cliente"
            nombre_cliente_match = re.search(r"Titular del contrato:\s*(.*?)\n", ocr_text)
            nombre_cliente = nombre_cliente_match.group(1).strip() if nombre_cliente_match else None

            # Extraer "numero_referencia"
            try:
                numero_referencia_match = re.search(
                    r"Referencia del contrato de sumi\n\ntro \(LIDERA COMERCIALIZADORA ENERGIA\):\s*(.+)",
                    ocr_text
                )
                numero_referencia = numero_referencia_match.group(1).strip() if numero_referencia_match else None
            except Exception as e:
                logger.error(f"Error al extraer 'numero_referencia': {str(e)}")
                numero_referencia = None

            # Extraer "fecha_emision"
            try:
                fecha_emision_match = re.search(
                    r"Fecha emi\n\nn factura:\s*(.+)",
                    ocr_text
                )
                if fecha_emision_match:
                    raw_fecha_emision = fecha_emision_match.group(1).strip()
                    # Utilizar la función de mapeo de meses
                    match = re.match(r"(\d{1,2})\s+de\s+(\w+)\s+de\s+(\d{4})", raw_fecha_emision)
                    if match:
                        dia, mes_texto, anio = match.groups()
                        mes = meses.get(mes_texto.lower())
                        if mes:
                            fecha_emision = f"{anio}-{mes}-{dia.zfill(2)}"
                        else:
                            fecha_emision = None
                    else:
                        fecha_emision = None
                else:
                    fecha_emision = None
            except Exception as e:
                logger.error(f"Error al extraer 'fecha_emision': {str(e)}")
                fecha_emision = None

            # Extraer "periodo_facturacion"
            try:
                periodo_match = re.search(
                    r"Periodo de consumo:\n\nDe\s*(\d{1,2}/\d{1,2}/\d{4})\s*al\s*(\d{1,2}/\d{1,2}/\d{4})",
                    ocr_text
                )
                if periodo_match:
                    raw_inicio = periodo_match.group(1).strip()
                    raw_fin = periodo_match.group(2).strip()
                    
                    # Convertir las fechas al formato YYYY-MM-DD
                    def convertir_fecha(fecha_raw):
                        try:
                            return datetime.strptime(fecha_raw, "%d/%m/%Y").strftime("%Y-%m-%d")
                        except ValueError:
                            logger.error(f"Error al convertir fecha: {fecha_raw}")
                            return None

                    inicio = convertir_fecha(raw_inicio)
                    fin = convertir_fecha(raw_fin)
                else:
                    inicio = None
                    fin = None
            except Exception as e:
                logger.error(f"Error al extraer 'periodo_facturacion': {str(e)}")
                inicio = None
                fin = None

            # Extraer "días"
            try:
                dias_match = re.search(r"(\d+)\s+Días", ocr_text)
                dias = int(dias_match.group(1).strip()) if dias_match else None
            except Exception as e:
                logger.error(f"Error al extraer 'días': {str(e)}")
                dias = None

            # Extraer "forma_pago"
            try:
                forma_pago_match = re.search(r"Forma de pago:\s*(.+)", ocr_text)
                forma_pago = forma_pago_match.group(1).strip() if forma_pago_match else None
            except Exception as e:
                logger.error(f"Error al extraer 'forma_pago': {str(e)}")
                forma_pago = None

            # Extraer "fecha_cargo"
            try:
                fecha_cargo_match = re.search(r"Fecha de cargo:\n\n(\d{2}/\d{2}/\d{4})", ocr_text)
                if fecha_cargo_match:
                    fecha_cargo_raw = fecha_cargo_match.group(1)
                    fecha_cargo = datetime.strptime(fecha_cargo_raw, "%d/%m/%Y").strftime("%Y-%m-%d")
                else:
                    fecha_cargo = None
            except Exception as e:
                logger.error(f"Error al extraer 'fecha_cargo': {str(e)}")
                fecha_cargo = None

            # Extraer "mandato"


            # Extraer "costo_potencia"
            def extract_costo_potencia(ocr_text):
                """
                Extrae y calcula el costo de potencia sumando los valores flotantes encontrados después de "Días"
                y antes del siguiente "€/KW día" o variantes como "E/KW día".
                """
                import re

                try:
                    # Localizar la sección en el OCR donde aparece "DETALLE DE LA FACTURA"
                    potencia_section_match = re.search(r"DETALLE DE LA FACTURA", ocr_text, re.IGNORECASE)
                    if not potencia_section_match:
                        print("No se encontró la sección 'DETALLE DE LA FACTURA' en el OCR.")
                        return 0.0  # Retornar 0 si no se encuentra la sección

                    # Limitar el análisis del OCR al texto después de "DETALLE DE LA FACTURA"
                    ocr_text_after_potencia = ocr_text[potencia_section_match.end():]

                    # Inicializar lista para valores encontrados
                    valores = []

                    # Buscar todas las ocurrencias relevantes en formato "Días ... €/KW día\n\n<valor>"
                    # Ahora también acepta "E/KW día" en lugar de "€/KW día"
                    dias_matches = re.finditer(
                        r"Días.*?[€E]/KW día\n\n([\d,\.]+)",  # Modificado para aceptar € o E
                        ocr_text_after_potencia,
                        re.IGNORECASE
                    )
                    for match in dias_matches:
                        valor_raw = match.group(1)  # Capturar el valor después de "€/KW día\n\n" o "E/KW día\n\n"
                        try:
                            valor = float(valor_raw.replace(",", "."))  # Convertir a float
                            valores.append(valor)  # Guardar el valor en la lista
                        except ValueError:
                            print(f"Valor inválido encontrado: {valor_raw}")

                    # Sumar todos los valores capturados
                    costo_potencia = round(sum(valores), 2)
                    return costo_potencia

                except Exception as e:
                    print(f"Error al calcular 'costo_potencia': {str(e)}")
                    return 0.0  # Valor predeterminado en caso de error


            # Extraer "costo_potencia"


            # Extraer "descuentos"
                

            # Extraer "impuestos"

            match = re.search(r"Impuesto Electricidad\n\n([\d.,]+)", ocr_text, re.IGNORECASE)
            impuestos = float(match.group(1).replace(",", ".")) if match else 0.0

            # Extraer "total_a_pagar"

            match = re.search(r"TOTAL IMPORTE FACTURA\n\n([\d.,]+)", ocr_text, re.IGNORECASE)
            if match:
                total_a_pagar_raw = match.group(1)
                total_a_pagar = float(total_a_pagar_raw.replace(".", "").replace(",", ".") if "," in total_a_pagar_raw else total_a_pagar_raw.replace(",", ""))
            else:
                total_a_pagar = 0.0


            # Extraer "consumo_punta"


            # Extraer "consumo_valle"
                

            # Extraer "consumo_total"
                

            # Extraer "precio_efectivo_energia"
                

            # Construir JSON
            parsed_data = {
                "nombre_cliente": nombre_cliente,
                "numero_referencia": numero_referencia,
                "fecha_emision": fecha_emision,
                "periodo_facturacion": {
                    "inicio": inicio,
                    "fin": fin,
                    "dias": dias,
                },
                "forma_pago": forma_pago,
                "fecha_cargo": fecha_cargo,
                "mandato": "SIN CODIGO DE MANDATO",
                "desglose_cargos": {
                    "costo_potencia": extract_costo_potencia(ocr_text),
                    "costo_energia": costo_energia,
                    "descuentos": 000,
                    "impuestos": impuestos,
                    "total_a_pagar": total_a_pagar,
                },
                "detalles_consumo": {
                    "consumo_punta": 000,
                    "consumo_valle": 000,
                    "consumo_total": 000,
                    "precio_efectivo_energia": 000,
                },
            }

            return parsed_data

        except Exception as e:
            logger.error(f"Error al convertir OCR a JSON para Lidera Energia: {str(e)}")
            return {
                "error": "Error al convertir OCR a JSON para Lidera Energia."
            }

    def extract_naturgy_data(self, ocr_text):
        """
        Extrae los datos específicos de las facturas de Naturgy a partir del texto OCR.
        """
        try:
            import re
            from datetime import datetime

            # Mapeo de meses en español a números
            meses = {
                "enero": "01", "febrero": "02", "marzo": "03", "abril": "04",
                "mayo": "05", "junio": "06", "julio": "07", "agosto": "08",
                "septiembre": "09", "octubre": "10", "noviembre": "11", "diciembre": "12"
            }

            # Inicializar valores por defecto
            nombre_cliente = None
            numero_referencia = None
            fecha_emision = None
            periodo_inicio = None
            periodo_fin = None
            dias = None
            forma_pago = None
            fecha_cargo = None
            mandato = None
            costo_potencia = None
            costo_energia = None
            descuentos = None
            impuestos = None
            total_a_pagar = None
            consumo_punta = None
            consumo_valle = None
            consumo_total = None
            precio_efectivo_energia = None

            # Extraer "nombre_cliente"
                

            # Extraer "numero_referencia"
                

            # Extraer "fecha_emision"
                

            # Extraer "periodo_facturacion"
                

            # Extraer "dias"


            # Extraer "forma_pago"
                

            # Extraer "fecha_cargo"
                

            # Extraer "mandato"


            # Extraer valor de "costo_energia" (antes de " €\n\nEnergia consumida")


            # Extraer "descuentos"
                

            # Extraer "impuestos"


            # Extraer segundo valor de "impuestos" (antes de "TOTAL IMPORTE FACTURA")
                

            # Calcular el valor total de "impuestos"


            # Extraer "total_a_pagar"


            # Extraer "consumo_punta"


            # Extraer "consumo_valle"
                

            # Extraer "consumo_total"
                

            # Extraer "precio_efectivo_energia"
                

            # Construir JSON
            parsed_data = {
                "nombre_cliente": "NATURGY TESTE",
                "numero_referencia": "XXXXXXXXX",
                "fecha_emision": "1990-01-01",
                "periodo_facturacion": {
                    "inicio": "1990-01-01",
                    "fin": "1990-01-01",
                    "dias": "00",
                },
                "forma_pago": "teste forma de pago",
                "fecha_cargo": "1990-01-01",
                "mandato": "XXXXXXXXX",
                "desglose_cargos": {
                    "costo_potencia": 000,
                    "costo_energia": 000,
                    "descuentos": 000,
                    "impuestos": 000,
                    "total_a_pagar": 000,
                },
                "detalles_consumo": {
                    "consumo_punta": 000,
                    "consumo_valle": 000,
                    "consumo_total": 000,
                    "precio_efectivo_energia": 000,
                },
            }

            return parsed_data

        except Exception as e:
            logger.error(f"Error al convertir OCR a JSON para Naturgy: {str(e)}")
            return {
                "error": "Error al convertir OCR a JSON para Naturgy."
            }
    
    def extract_edistribucion_data(self, ocr_text):
        """
        Extrae los datos específicos de las facturas de E-Distribución a partir del texto OCR.
        """
        try:
            import re
            from datetime import datetime

            # Mapeo de meses en español a números
            meses = {
                "enero": "01", "febrero": "02", "marzo": "03", "abril": "04",
                "mayo": "05", "junio": "06", "julio": "07", "agosto": "08",
                "septiembre": "09", "octubre": "10", "noviembre": "11", "diciembre": "12"
            }

            # Inicializar valores por defecto
            nombre_cliente = None
            numero_referencia = None
            fecha_emision = None
            periodo_inicio = None
            periodo_fin = None
            dias = None
            forma_pago = None
            fecha_cargo = None
            mandato = None
            costo_potencia = None
            costo_energia = None
            descuentos = None
            impuestos = None
            total_a_pagar = None
            consumo_punta = None
            consumo_valle = None
            consumo_total = None
            precio_efectivo_energia = None

            # Extraer "nombre_cliente"
                

            # Extraer "numero_referencia"
                

            # Extraer "fecha_emision"
                

            # Extraer "periodo_facturacion"
                

            # Extraer "dias"


            # Extraer "forma_pago"
                

            # Extraer "fecha_cargo"
                

            # Extraer "mandato"


            # Extraer valor de "costo_energia" (antes de " €\n\nEnergia consumida")


            # Extraer "descuentos"
                

            # Extraer "impuestos"


            # Extraer segundo valor de "impuestos" (antes de "TOTAL IMPORTE FACTURA")
                

            # Calcular el valor total de "impuestos"


            # Extraer "total_a_pagar"


            # Extraer "consumo_punta"


            # Extraer "consumo_valle"
                

            # Extraer "consumo_total"
                

            # Extraer "precio_efectivo_energia"
                

            # Construir JSON
            parsed_data = {
                "nombre_cliente": "E-DISTRIBUCIÓN TESTE",
                "numero_referencia": "XXXXXXXXX",
                "fecha_emision": "1990-01-01",
                "periodo_facturacion": {
                    "inicio": "1990-01-01",
                    "fin": "1990-01-01",
                    "dias": "00",
                },
                "forma_pago": "teste forma de pago",
                "fecha_cargo": "1990-01-01",
                "mandato": "XXXXXXXXX",
                "desglose_cargos": {
                    "costo_potencia": 000,
                    "costo_energia": 000,
                    "descuentos": 000,
                    "impuestos": 000,
                    "total_a_pagar": 000,
                },
                "detalles_consumo": {
                    "consumo_punta": 000,
                    "consumo_valle": 000,
                    "consumo_total": 000,
                    "precio_efectivo_energia": 000,
                },
            }

            return parsed_data

        except Exception as e:
            logger.error(f"Error al convertir OCR a JSON para E-Distribución: {str(e)}")
            return {
                "error": "Error al convertir OCR a JSON para E-Distribución."
            }

################################################################################################################################
############################################ GET - VISUALIZAR FATURA POR ID ####################################################
################################################################################################################################


from .serializers import InvoiceSerializer
from voltix.utils.comparison_status import annotate_comparison_status
from django.http import JsonResponse

class InvoiceDetailView(APIView):
    permission_classes = [IsAuthenticated]

    @swagger_auto_schema(
        operation_summary="Obtener factura por ID con estado de comparación",
        operation_description=(
            "Permite a un usuario autenticado obtener los detalles de una factura específica por su ID, "
            "siempre y cuando la factura pertenezca al usuario autenticado. "
            "También incluye el estado de comparación relacionado."
        ),
        responses={
            200: openapi.Response(
                description="Detalles de la factura obtenidos exitosamente, incluido el estado de comparación.",
                examples={
                    "application/json": {
                        "id": 123,
                        "user": {"id": 1, "username": "usuario"},
                        "billing_period_start": "2023-01-01",
                        "billing_period_end": "2023-01-31",
                        "data": {
                            "nombre_cliente": "Ejemplo Cliente",
                            "detalles_consumo": {
                                "consumo_punta": 120.5,
                                "consumo_valle": 85.4,
                                "total_kwh": 205.9
                            }
                        },
                        "comparison_status": "Sin discrepancia"
                    }
                },
            ),
            404: openapi.Response(
                description="Factura no encontrada o no pertenece al usuario autenticado.",
                examples={
                    "application/json": {
                        "detail": "Factura con ID 123 no encontrada."
                    }
                },
            ),
            401: openapi.Response(
                description="No autenticado.",
                examples={
                    "application/json": {
                        "detail": "No se han proporcionado credenciales de autenticación."
                    }
                },
            ),
        },
        manual_parameters=[
            openapi.Parameter(
                name="invoice_id",
                in_=openapi.IN_PATH,
                description="ID de la factura que se desea obtener.",
                type=openapi.TYPE_INTEGER,
                required=True,
            )
        ]
    )

    def get(self, request, invoice_id):
        try:
            invoice_queryset = Invoice.objects.filter(pk=invoice_id, user=request.user)

            if not invoice_queryset.exists():
                return JsonResponse(
                    {"error": f"Factura con ID {invoice_id} no encontrada."},
                    status=status.HTTP_404_NOT_FOUND,
                )

            annotated_invoice = annotate_comparison_status(invoice_queryset, "invoice").first()

            if not annotated_invoice:
                return JsonResponse(
                    {"detail": "Factura no encontrada después de la anotación."},
                    status=status.HTTP_404_NOT_FOUND,
                )

            serializer = InvoiceSerializer(annotated_invoice)
            response_data = serializer.data
            response_data["comparison_status"] = annotated_invoice.comparison_status

            return Response(response_data, status=status.HTTP_200_OK)

        except Exception as e:
            return JsonResponse(
                {
                    "error": "Ocurrió un error al obtener los detalles de la factura.",
                    "details": str(e),
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


class InvoiceImageView(APIView):
    """
    Endpoint para obtener la URL de la imagen de una factura por ID.
    """
    permission_classes = [IsAuthenticated]

    @swagger_auto_schema(
        operation_summary="Obtener la URL de la imagen de una factura",
        operation_description=(
            "Permite a un usuario autenticado obtener la URL de la imagen asociada a una factura específica. "
            "La factura debe pertenecer al usuario autenticado."
        ),
        responses={
            200: openapi.Response(
                description="URL de la imagen de la factura obtenida exitosamente.",
                examples={
                    "application/json": {
                        "status": "success",
                        "image_url": "https://example.com/images/factura123.jpg"
                    }
                },
            ),
            404: openapi.Response(
                description="Factura no encontrada o no pertenece al usuario autenticado.",
                examples={
                    "application/json": {
                        "status": "error",
                        "message": "No se encontró una factura con ID 123."
                    }
                },
            ),
            401: openapi.Response(
                description="No autenticado.",
                examples={
                    "application/json": {
                        "detail": "No se han proporcionado credenciales de autenticación."
                    }
                },
            ),
        },
        manual_parameters=[
            openapi.Parameter(
                name="invoice_id",
                in_=openapi.IN_PATH,
                description="ID de la factura para obtener su URL de imagen.",
                type=openapi.TYPE_INTEGER,
                required=True,
            )
        ]
    )
    def get(self, request, invoice_id):
        try:
            # Buscar la factura por ID y verificar que pertenece al usuario autenticado
            invoice = Invoice.objects.filter(id=invoice_id, user=request.user).first()

            if not invoice:
                return Response(
                    {"status": "error", "message": f"No se encontró una factura con ID {invoice_id}."},
                    status=status.HTTP_404_NOT_FOUND,
                )

            # Devolver la URL de la imagen
            return Response(
                {"status": "success", "image_url": invoice.image_url},
                status=status.HTTP_200_OK,
            )

        except Exception as e:
            return Response(
                {"status": "error", "message": "Ocurrió un error al obtener la imagen de la factura.", "details": str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
