from django.http import JsonResponse
from voltix.models import Usuario
from django.views.decorators.csrf import csrf_exempt
from django.http import HttpResponse

def index(request):
    return HttpResponse("Bienvenido a Users.")

@csrf_exempt
def get_all_users(request):
    if request.method == 'GET':
        try:
            # Obtener todos los usuarios
            usuarios = Usuario.objects.all().values(
                'user_id', 'fullname', 'dni', 'email', 'created_at', 'updated_at'
            )
            usuarios_list = list(usuarios)

            # Respuesta JSON con los usuarios
            return JsonResponse({
                "message": "Usuarios obtenidos exitosamente",
                "usuarios": usuarios_list
            }, status=200)

        except Exception as e:
            return JsonResponse({"error": f"Ocurrió un error: {str(e)}"}, status=500)

    return JsonResponse({"error": "Método no permitido"}, status=405)
