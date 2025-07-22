from django.http import JsonResponse
from rest_framework_simplejwt.authentication import JWTAuthentication
from django.utils.deprecation import MiddlewareMixin

class JWTValidationMiddleware(MiddlewareMixin):
    def process_request(self, request):
        if request.path in ['/api/users/login/', '/api/users/register/']:
            return None

        if request.path.startswith('/api/'):
            auth = JWTAuthentication()
            header = auth.get_header(request)

            if header is None:
                return JsonResponse({
                    'error': 'No Authorization header provided',
                    'redirect_to': '/sign-in',
                    'message': 'No hay token de autenticación. Redirigiendo a login...'
                }, status=401)

            try:
                # Decodifica si está en bytes
                if isinstance(header, bytes):
                    header = header.decode('utf-8')

                print("Validando JWT...", header)

                # Quita el prefijo 'Bearer ' para obtener solo el token
                if header.startswith("Bearer "):
                    raw_token = header.split("Bearer ")[1]
                else:
                    return JsonResponse({
                        'error': 'Invalid Authorization header format',
                        'redirect_to': '/sign-in',
                        'message': 'Formato de token inválido. Redirigiendo a login...'
                    }, status=401)

                validated_token = auth.get_validated_token(raw_token)
                user = auth.get_user(validated_token)
                request.user = user

            except Exception as e:
                print("Error al validar el token JWT:", str(e))
                return JsonResponse({
                    'error': 'Invalid or expired token',
                    'details': str(e),
                    'redirect_to': '/sign-in',
                    'message': 'Token expirado o inválido. Redirigiendo a login...'
                }, status=401)

        return None
