with open('.env', 'r') as f:
    lines = f.readlines()

new_lines = []
for line in lines:
    if line.startswith('STRIPE_SECRET_KEY='):
        # Wrap in single quotes to prevent $ expansion
        new_lines.append("STRIPE_SECRET_KEY='sk_test_51Mwso9FouqpVToviZKu8f3oQA7wepnncseUPJyr5ACLNZAS8rZIn$D6j2jjiZf7mxq9et0t5l7TXyDndbT9i2lelhFqcyjjYxxp00ZRqI9xqE'\n")
    else:
        new_lines.append(line)

with open('.env', 'w') as f:
    f.writelines(new_lines)

print("Fixed!")
