from scrapper import run_scrapper

tipo = input("Digite o tipo de produto que deseja buscar: ")
run_scrapper(tipo)


'''
tipos de erros pra lidar dps do gerador di link muahahaha
- LoginError: para quando o usuário precisa fazer login manualmente
- ValueError: se o link tivé cagado ou o layout do ML tiver mudado e o script não conseguir extrair o link corretamente

'''